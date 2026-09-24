"""Office state: one read-only snapshot of who is doing what, for the Office view.

``GET /api/office/state`` merges two sources into a single payload:

* Mission Control's own managers (task manager, job manager, routing history,
  memory store, handoff manager, account registry), passed in by the caller.
* The shared-brain swarm pool, read **read-only** from
  ``$BRAIN_DIR/swarm/tasks/{pending,in-progress,completed,escalated}/*.json`` and
  the baton at ``$BRAIN_DIR/handoff/current.md``.

Everything here is a pure function of its arguments so it can be unit-tested
without a server. Every string that leaves this module is treated as untrusted:
it is passed through the SecretRedactor *before* it is truncated (truncating
first could cut a secret below the pattern's minimum length and leak its
prefix), task output bodies are never copied, and error text only ever appears
as a short redacted ``detail``. HTML escaping is the client's job.

Filesystem reads of the brain store are bounded: a capped number of files per
state folder, a byte cap per file, a total byte budget, no symlink that resolves
outside BRAIN_DIR, and no non-regular file (a FIFO would block the request).
"""
from __future__ import annotations

import datetime
import heapq
import json
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

# --- Limits ------------------------------------------------------------------

MAX_TASKS = 60
MAX_FLOWS = 100
MAX_MEMORY_RECENT = 10
TITLE_CHARS = 140
DETAIL_CHARS = 160
FIELD_CHARS = 200          # ids, models, agent names, branches
REDACT_INPUT_CHARS = 4096  # bound regex work per string; output is far shorter
#: Whitespace is collapsed over at most this much of a value before the
#: REDACT_INPUT_CHARS cut (brain files are already capped at MAX_FILE_BYTES).
CLEAN_SCAN_CHARS = 1024 * 1024
MAX_ATTEMPTS = 10

#: Newest files read per swarm state folder, and entries scanned per folder.
#: Task ids are random, so directory order says nothing about age: every
#: scanned entry is ranked by mtime. A folder beyond MAX_DIR_ENTRIES (an
#: unpruned completed/ after months of use) is ranked on the first
#: MAX_DIR_ENTRIES entries only; the snapshot counts such folders in
#: sources.truncated_dirs so a possibly missing newer task is visible, not silent.
MAX_FILES_PER_STATE = 80
MAX_DIR_ENTRIES = 20000
#: Swarm task JSON embeds the agent's full output, so allow a generous cap per
#: file but bound the whole snapshot.
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_HANDOFF_BYTES = 64 * 1024
MAX_ROUTING_TAIL_BYTES = 256 * 1024

ESCALATION_BLOCK_SECONDS = 600
CACHE_TTL_SECONDS = 1.0

#: Swarm state folder -> contract stage. Both spellings of in-progress are
#: accepted; scripts/brain/swarm.py writes the hyphenated one.
SWARM_STATES = ("pending", "in-progress", "in_progress", "completed", "escalated")

#: Swarm worker ids and their display names.
SWARM_AGENT_LABELS = {
    "kiro-cli": "Kiro CLI",
    "cline": "Cline",
    "antigravity": "Antigravity",
    "antigravity-api": "Antigravity (API key)",
    "antigravity-ide": "Antigravity IDE",
}
#: In-editor agents the swarm only *delivers* to (never evidence of execution).
DELIVERY_AGENTS = frozenset({"antigravity-ide"})

AGENT_KINDS = ("kiro-cli", "cline", "antigravity", "antigravity-api", "antigravity-ide", "openhands", "api", "other")

_OFFLINE_ACCOUNT_STATUSES = frozenset({"DISABLED", "OFFLINE", "AUTH_ERROR", "CONFIG_ERROR", "NOT_CONFIGURED"})
_BLOCKED_ACCOUNT_STATUSES = frozenset({"RATE_LIMITED", "QUOTA_EXHAUSTED", "COOLDOWN", "DEGRADED"})

_MC_STAGE = {
    "BACKLOG": "queued",
    "READY": "queued",
    "PLANNING": "routing",
    "RUNNING": "running",
    "REVIEW": "review",
    "COMPLETED": "done",
    "VERIFICATION_COMPLETE": "done",
    "BLOCKED": "escalated",
    "FAILED": "escalated",
    "CANCELLED": "escalated",
    "DEPTH_LIMIT_REACHED": "escalated",
    "BUDGET_EXHAUSTED": "escalated",
    "REJECTED": "escalated",
}
_JOB_STAGE = {"pending": "queued", "running": "running", "completed": "done", "failed": "escalated", "cancelled": "escalated"}

_WS_RE = re.compile(r"\s+")
# Matched against a stripped, length-capped line (see _handoff_line) so neither
# pattern needs a trailing \s* or a lazy group; both stay linear on hostile input.
_OBS_RE = re.compile(r"^-\s*\[(status|agent|next)\]\s*(.*)$", re.I)
_H1_RE = re.compile(r"^#\s+(\S.*)$")
HANDOFF_LINE_CHARS = 2000


# --- String hygiene ----------------------------------------------------------

class _NullRedactor:
    def redact(self, text: str) -> str:
        return text


def clean(value: Any, redactor: Any, limit: int = FIELD_CHARS) -> str | None:
    """Redact, collapse whitespace and truncate one untrusted value (None stays None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        value = "true" if value else "false"
    redact = (redactor or _NullRedactor()).redact
    # Redact the raw text first: the redactor's registered-secret check is an
    # exact substring match, and a secret containing whitespace would no longer
    # match after the collapse below. The second pass after cutting catches
    # pattern-shaped secrets that padding had pushed past the scan window.
    raw = redact(str(value)[:CLEAN_SCAN_CHARS * 2])
    # Collapse whitespace *before* bounding the redactor's input. Cutting first
    # let padding (4000 spaces, then a token) cut a secret below the pattern's
    # minimum length and the collapse then pulled that fragment into view.
    text = _WS_RE.sub(" ", raw[:CLEAN_SCAN_CHARS]).strip()
    if len(raw) > CLEAN_SCAN_CHARS or len(text) > REDACT_INPUT_CHARS:
        # Whatever token straddles a cut is dropped whole: a partial secret
        # can be too short for the redactor to recognise. Tokens never contain
        # whitespace, so the last space bounds the last complete one.
        head = text[:REDACT_INPUT_CHARS]
        head = head.rsplit(" ", 1)[0] if " " in head else ""
        text = (head + " \u2026").strip()
    text = redact(text)
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "\u2026"
    return text or None


def _number(value: Any, lo: float | None = None, hi: float | None = None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):
        return None
    if lo is not None:
        num = max(lo, num)
    if hi is not None:
        num = min(hi, num)
    return round(num, 3)


def parse_ts(value: Any) -> datetime.datetime | None:
    """Parse an ISO-8601 timestamp (naive means UTC); None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip()[:64].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        # An extreme offset ("0001-01-01T00:00:00+05:00") overflows here.
        return dt.astimezone(datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


def iso(value: Any) -> str | None:
    dt = value if isinstance(value, datetime.datetime) else parse_ts(value)
    return dt.isoformat() if dt else None


def agent_kind(agent_id: Any, provider_id: Any = None, account_type: Any = None) -> str | None:
    """Map an agent/account id onto the contract's agent kind."""
    if not agent_id and not provider_id:
        return None
    aid = str(agent_id or "").lower()
    pid = str(provider_id or "").lower()
    if aid in ("antigravity-ide", "antigravity-api"):
        return aid
    if aid == "kiro-cli" or pid == "kiro" or aid.startswith("kiro"):
        return "kiro-cli"
    if pid == "cline" or aid.startswith("cline"):
        return "cline"
    if pid == "antigravity" or aid.startswith("antigravity"):
        return "antigravity"
    if pid == "openhands" or aid.startswith("openhands"):
        return "openhands"
    if str(account_type or "").lower() in ("api", "gateway") or pid in ("openai", "anthropic", "gemini", "ollama"):
        return "api"
    return "other"


# --- Bounded brain-store reads -----------------------------------------------

class _Budget:
    def __init__(self, total: int) -> None:
        self.remaining = total
        self.skipped = 0
        self.truncated_dirs = 0


def _within(path: Path, root: Path) -> Path | None:
    """Resolved path if it stays inside root, else None (symlink escapes)."""
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    return resolved


def read_bounded(path: Path, root: Path, max_bytes: int, budget: _Budget | None = None) -> bytes | None:
    """Read a regular file inside root, at most max_bytes; None if refused."""
    resolved = _within(path, root)
    if resolved is None:
        if budget:
            budget.skipped += 1
        return None
    try:
        st = resolved.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise OSError("not a bounded regular file")
        if budget is not None and st.st_size > budget.remaining:
            raise OSError("snapshot byte budget exhausted")
        with open(resolved, "rb") as fh:
            data = fh.read(max_bytes + 1)
    except OSError:
        if budget:
            budget.skipped += 1
        return None
    if len(data) > max_bytes:
        if budget:
            budget.skipped += 1
        return None
    if budget is not None:
        budget.remaining -= len(data)
    return data


def _newest_json_files(folder: Path, root: Path, budget: _Budget | None = None) -> list[Path]:
    """Newest *.json files in folder (capped), skipping anything outside root."""
    if _within(folder, root) is None:
        return []
    entries: list[tuple[float, str]] = []
    try:
        with os.scandir(folder) as it:
            for i, entry in enumerate(it):
                if i >= MAX_DIR_ENTRIES:
                    if budget is not None:
                        budget.truncated_dirs += 1
                    break
                if not entry.name.endswith(".json"):
                    continue
                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                entries.append((mtime, entry.path))
    except OSError:
        return []
    return [Path(p) for _, p in heapq.nlargest(MAX_FILES_PER_STATE, entries)]


def read_swarm_records(brain_root: Path, budget: _Budget | None = None) -> tuple[list[tuple[str, dict]], int]:
    """(state, record) pairs from the swarm pool, plus the count of skipped files."""
    if not brain_root:
        return [], 0
    root = Path(brain_root).resolve()
    tasks_dir = root / "swarm" / "tasks"
    budget = budget or _Budget(MAX_TOTAL_BYTES)
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for state in SWARM_STATES:
        for path in _newest_json_files(tasks_dir / state, root, budget):
            raw = read_bounded(path, root, MAX_FILE_BYTES, budget)
            if raw is None:
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            # RecursionError: "[" * 200000 is valid-looking input that blows
            # the decoder's stack; one such file must not take the endpoint down.
            except (UnicodeDecodeError, ValueError, RecursionError):
                budget.skipped += 1
                continue
            if not isinstance(rec, dict):
                budget.skipped += 1
                continue
            tid = rec.get("id") or rec.get("task_id") or path.stem
            if not isinstance(tid, str) or tid in seen:
                continue
            seen.add(tid)
            rec["id"] = tid
            out.append(("in-progress" if state == "in_progress" else state, rec))
    return out, budget.skipped


def read_brain_handoff(brain_root: Path, redactor: Any) -> dict | None:
    """Parse $BRAIN_DIR/handoff/current.md (H1 title plus status/agent/next)."""
    root = brain_root.resolve() if brain_root else None
    if root is None:
        return None
    path = root / "handoff" / "current.md"
    raw = read_bounded(path, root, MAX_HANDOFF_BYTES)
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    found: dict[str, str] = {}
    title = None
    in_front_matter = False
    for n, line in enumerate(text.splitlines()):
        if n == 0 and line.strip() == "---":
            in_front_matter = True
            continue
        if in_front_matter:
            if line.strip() == "---":
                in_front_matter = False
            continue
        line = line[:HANDOFF_LINE_CHARS].strip()
        if title is None:
            m = _H1_RE.match(line)
            if m:
                title = m.group(1)
                continue
        m = _OBS_RE.match(line)
        if m and m.group(1).lower() not in found:
            found[m.group(1).lower()] = m.group(2)
    try:
        mtime = datetime.datetime.fromtimestamp((_within(path, root) or path).stat().st_mtime, datetime.timezone.utc)
    except (OSError, ValueError, OverflowError):
        mtime = None
    status = clean(found.get("status"), redactor, 40)
    return {
        "title": clean(title, redactor, TITLE_CHARS),
        # "[status] done — archived" is invalid per the schema; keep the first word.
        "status": status.split()[0].lower() if status else None,
        "agent": clean(found.get("agent"), redactor),
        "next": clean(found.get("next"), redactor, DETAIL_CHARS),
        "updated_at": iso(mtime),
        "source": "brain",
    }


def read_jsonl_tail(path: Path | None, limit: int = 50, max_bytes: int = MAX_ROUTING_TAIL_BYTES) -> list[dict]:
    """Newest-first dict records from the tail of a JSONL file (bounded read)."""
    if not path:
        return []
    try:
        path = Path(path)
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return []
        with open(path, "rb") as fh:
            if st.st_size > max_bytes:
                fh.seek(st.st_size - max_bytes)
                fh.readline()  # drop the partial first line
            data = fh.read(max_bytes)
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(data.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            out.append(item)
            if len(out) >= limit:
                break
    return out


# --- Normalisers -------------------------------------------------------------

def _latest_ts(task: dict) -> datetime.datetime:
    best = None
    for key in ("completed_at", "started_at", "created_at"):
        dt = parse_ts(task.get(key))
        if dt and (best is None or dt > best):
            best = dt
    return best or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _duration(started: Any, completed: Any, explicit: Any) -> float | None:
    num = _number(explicit, lo=0.0)
    if num:
        return num
    t0, t1 = parse_ts(started), parse_ts(completed)
    if t0 and t1 and t1 >= t0:
        return round((t1 - t0).total_seconds(), 3)
    return None


def _swarm_stage(state: str, rec: dict) -> str:
    if state == "pending":
        return "queued"
    if state == "completed":
        return "done"
    if state == "escalated":
        return "escalated"
    stage_state = str(rec.get("stage_state") or "")
    # Delivery lifecycle (scripts/brain/swarm.py): staged_for_<agent> ->
    # acknowledged_by_<agent> -> working -> awaiting_verification. Staging and
    # acknowledgement are receipt, not execution (AGENTS.md), so both stay
    # "delivered"; only "working" is evidence the agent is running the task.
    if stage_state.startswith(("staged", "acknowledged")) or (
            rec.get("assigned_to") in DELIVERY_AGENTS and not stage_state):
        return "delivered"
    if stage_state == "awaiting_verification":
        return "review"
    return "running"


def normalize_swarm_task(state: str, rec: dict, redactor: Any) -> dict:
    agent = rec.get("assigned_to") or rec.get("assigned_agent")
    rec_model = rec.get("model_recommendation") if isinstance(rec.get("model_recommendation"), dict) else {}
    sandbox = rec.get("sandbox") if isinstance(rec.get("sandbox"), dict) else {}
    sandbox_result = rec.get("sandbox_result") if isinstance(rec.get("sandbox_result"), dict) else {}
    attempts_raw = rec.get("antigravity_attempts")
    attempts = [clean(a, redactor, DETAIL_CHARS) for a in attempts_raw[:MAX_ATTEMPTS]] if isinstance(attempts_raw, list) else []
    # Only a model the agent actually ran on (or reported). A delivery's
    # recommendation is surfaced as model_tier, never presented as the model used.
    model = rec.get("model") or rec.get("model_reported")
    title = rec.get("title") or rec.get("instruction") or rec.get("prompt") or "Untitled task"
    return {
        "id": clean(rec.get("id"), redactor),
        "title": clean(title, redactor, TITLE_CHARS),
        "stage": _swarm_stage(state, rec),
        "agent": clean(agent, redactor),
        "kind": agent_kind(agent),
        "model": clean(model, redactor),
        "model_tier": clean(rec_model.get("tier") or rec.get("model_recommended_tier") or rec.get("effort"), redactor, 40),
        "complexity": clean(rec.get("complexity"), redactor, 40),
        "risk": clean(rec.get("risk"), redactor, 40),
        "routing_reason": clean(rec.get("rationale") or rec.get("model_rationale"), redactor, DETAIL_CHARS),
        "confidence": _number(rec.get("confidence"), 0.0, 1.0),
        "requires_approval": bool(rec.get("requires_approval") or rec_model.get("requires_approval")),
        "created_at": iso(rec.get("created_at")),
        # Delivery is not execution, so staged_at never stands in for started_at.
        "started_at": iso(rec.get("started_at")),
        "completed_at": iso(rec.get("completed_at")),
        "duration_s": _duration(rec.get("started_at"), rec.get("completed_at"), rec.get("duration_seconds")),
        "sandbox_branch": clean(sandbox.get("branch") or sandbox_result.get("branch"), redactor),
        "attempts": [a for a in attempts if a],
        "account": clean(rec.get("antigravity_account"), redactor),
        "source": "swarm",
        # Internal, stripped before the response: short redacted error summary.
        "_error": clean(rec.get("error"), redactor, DETAIL_CHARS) if state == "escalated" else None,
        "_staged_at": iso(rec.get("staged_at") or (rec.get("updated_at") if str(rec.get("stage_state") or "").startswith("staged") else None)),
        "_acknowledged": str(rec.get("stage_state") or "").startswith("acknowledged"),
    }


def normalize_mc_task(task: Any, redactor: Any) -> dict:
    status = getattr(getattr(task, "status", None), "value", getattr(task, "status", None))
    agent = getattr(task, "assigned_agent", None) or getattr(task, "assigned_account", None)
    errors = getattr(task, "errors", None) or []
    return {
        "id": clean(getattr(task, "task_id", None), redactor),
        "title": clean(getattr(task, "title", None) or "Untitled task", redactor, TITLE_CHARS),
        "stage": _MC_STAGE.get(str(status), "queued"),
        "agent": clean(agent, redactor),
        "kind": agent_kind(agent),
        "model": clean(getattr(task, "actual_model", None) or getattr(task, "assigned_model", None), redactor),
        "model_tier": None,
        "complexity": clean(getattr(task, "complexity", None), redactor, 40),
        "risk": None,
        "routing_reason": None,
        "confidence": None,
        "requires_approval": bool(getattr(task, "requires_approval", False)),
        "created_at": iso(getattr(task, "created_at", None)),
        "started_at": iso(getattr(task, "started_at", None)),
        "completed_at": iso(getattr(task, "completed_at", None)),
        "duration_s": _duration(getattr(task, "started_at", None), getattr(task, "completed_at", None),
                                getattr(task, "duration_seconds", None)),
        "sandbox_branch": None,
        "attempts": [],
        "account": clean(getattr(task, "assigned_account", None), redactor),
        "source": "mission-control",
        "_error": clean(errors[-1], redactor, DETAIL_CHARS) if errors and _MC_STAGE.get(str(status)) == "escalated" else None,
        "_staged_at": None,
        "_acknowledged": False,
    }


def normalize_job(job: Any, redactor: Any) -> dict:
    meta = getattr(job, "metadata", None) or {}
    decision = meta.get("routing_decision") if isinstance(meta.get("routing_decision"), dict) else {}
    agent = getattr(job, "worker", None) or getattr(job, "account", None) or None
    history = meta.get("failover_history") if isinstance(meta.get("failover_history"), list) else []
    attempts = []
    for rec in history[:MAX_ATTEMPTS]:
        if isinstance(rec, dict):
            attempts.append(clean(f"{rec.get('original_account') or rec.get('original_provider')}: "
                                  f"{rec.get('reason') or 'failed'}", redactor, DETAIL_CHARS))
    status = str(getattr(job, "status", "") or "")
    return {
        "id": clean(getattr(job, "id", None), redactor),
        "title": clean(getattr(job, "task", None) or "Untitled job", redactor, TITLE_CHARS),
        "stage": _JOB_STAGE.get(status, "queued"),
        "agent": clean(agent, redactor),
        "kind": agent_kind(agent, getattr(job, "provider", None)),
        "model": clean(getattr(job, "model", None), redactor),
        "model_tier": None,
        "complexity": clean(decision.get("complexity"), redactor, 40),
        "risk": None,
        "routing_reason": clean(decision.get("reason"), redactor, DETAIL_CHARS),
        "confidence": None,
        "requires_approval": False,
        "created_at": iso(getattr(job, "created_at", None)),
        "started_at": iso(getattr(job, "started_at", None)),
        "completed_at": iso(getattr(job, "completed_at", None)),
        "duration_s": _duration(getattr(job, "started_at", None), getattr(job, "completed_at", None),
                                getattr(job, "duration", None)),
        "sandbox_branch": None,
        "attempts": [a for a in attempts if a],
        "account": clean(getattr(job, "account", None), redactor),
        "source": "mission-control",
        "_error": clean(getattr(job, "error", None), redactor, DETAIL_CHARS) if status == "failed" else None,
        "_staged_at": None,
        "_acknowledged": False,
    }


# --- Flows -------------------------------------------------------------------

def _flow(ts: Any, ftype: str, task_id: Any, src: Any, dst: Any, detail: Any, redactor: Any) -> dict | None:
    dt = ts if isinstance(ts, datetime.datetime) else parse_ts(ts)
    if dt is None:
        return None
    return {
        "ts": dt.isoformat(),
        "type": ftype,
        "task_id": clean(task_id, redactor),
        "from": clean(src, redactor),
        "to": clean(dst, redactor),
        "detail": clean(detail, redactor, DETAIL_CHARS) or "",
    }


def task_flows(task: dict, redactor: Any) -> list[dict]:
    """dispatch/route/deliver/start/failover/finish/escalate events for one task."""
    tid, agent = task["id"], task["agent"]
    out = [
        _flow(task["created_at"], "dispatch", tid, None, "queue", task["title"], redactor),
    ]
    if agent:
        reason = task["routing_reason"] or f"routed to {agent}"
        out.append(_flow(task["created_at"], "route", tid, "queue", agent, reason, redactor))
    if task["stage"] == "delivered" or (task["kind"] == "antigravity-ide" and task.get("_staged_at")):
        detail = ("acknowledged; no progress reported yet" if task.get("_acknowledged")
                  else "delivered; awaiting in-editor acknowledgement")
        out.append(_flow(task.get("_staged_at") or task["started_at"] or task["created_at"], "deliver",
                         tid, "queue", agent, detail, redactor))
    elif task["started_at"]:
        out.append(_flow(task["started_at"], "start", tid, "queue", agent, task["model"] or "started", redactor))
    # Failover trail: "acct: reason on model" entries; each failed one hands to the next.
    attempts = task["attempts"]
    fo_ts = task["started_at"] or task["created_at"]
    for i, entry in enumerate(attempts[:-1] if len(attempts) > 1 else []):
        src = entry.split(":", 1)[0].strip() if entry else None
        dst = attempts[i + 1].split(":", 1)[0].strip() if attempts[i + 1] else None
        out.append(_flow(fo_ts, "failover", tid, src, dst, entry, redactor))
    if task["completed_at"]:
        if task["stage"] == "escalated":
            out.append(_flow(task["completed_at"], "escalate", tid, agent, "escalated",
                             task.get("_error") or "escalated", redactor))
        elif task["stage"] == "done":
            dur = f" in {task['duration_s']:.0f}s" if task["duration_s"] else ""
            out.append(_flow(task["completed_at"], "finish", tid, agent, "done", f"completed{dur}", redactor))
    return [f for f in out if f]


def routing_flows(history: Iterable[dict], redactor: Any, known_task_ids: set[str] = frozenset()) -> list[dict]:
    """Route events from the router's decision log.

    A decision for a job already listed in ``tasks`` is skipped: that task's own
    route flow already represents it, and one decision should animate once.
    """
    out = []
    for rec in history:
        job_id = rec.get("job_id") if isinstance(rec, dict) else None
        if not isinstance(rec, dict) or (isinstance(job_id, str) and job_id in known_task_ids):
            continue
        out.append(_flow(rec.get("timestamp"), "route", job_id if isinstance(job_id, str) and job_id else None, "router",
                         rec.get("selected_account") or rec.get("selected_agent"),
                         rec.get("reason") or rec.get("task_text"), redactor))
    return [f for f in out if f]


def _flow_sort_key(flow: dict) -> datetime.datetime:
    return parse_ts(flow["ts"]) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


# --- Agents ------------------------------------------------------------------

def _agent_entry(agent_id: str, label: Any, kind: str | None, account: Any, source: str,
                 base_status: str, redactor: Any) -> dict:
    return {
        "id": clean(agent_id, redactor),
        "label": clean(label or agent_id, redactor, 80),
        "kind": kind if kind in AGENT_KINDS else "other",
        "account": clean(account, redactor),
        "status": base_status,
        "current_task_id": None,
        "model": None,
        "source": source,
    }


def _account_base_status(acc: Any) -> str:
    status = str(getattr(getattr(acc, "status", None), "value", getattr(acc, "status", "")) or "").upper()
    if not getattr(acc, "enabled", True) or status in _OFFLINE_ACCOUNT_STATUSES:
        return "offline"
    if status in _BLOCKED_ACCOUNT_STATUSES:
        return "blocked"
    return "idle"


def build_agents(accounts: Iterable[Any], tasks: list[dict], brain_swarm: bool,
                 now: datetime.datetime, redactor: Any) -> list[dict]:
    agents: dict[str, dict] = {}
    for acc in accounts or []:
        aid = getattr(acc, "id", None)
        if not aid or aid in agents:
            continue
        kind = agent_kind(aid, getattr(acc, "provider_id", None), getattr(acc, "account_type", None))
        agents[aid] = _agent_entry(aid, getattr(acc, "display_name", None) or aid, kind, aid,
                                   "mission-control", _account_base_status(acc), redactor)
    # Swarm workers: the canonical five when the pool exists, plus any other
    # assignee seen in a task. A swarm id that is also a registry account id
    # (kiro-cli) is one worker and keeps its registry entry.
    swarm_ids = list(SWARM_AGENT_LABELS) if brain_swarm else []
    swarm_ids += [t["agent"] for t in tasks if t["source"] == "swarm" and t["agent"]]
    for sid in swarm_ids:
        if sid not in agents:
            agents[sid] = _agent_entry(sid, SWARM_AGENT_LABELS.get(sid, sid), agent_kind(sid), None,
                                       "swarm", "idle", redactor)

    # Tasks are newest first, so the first match per agent is its latest task.
    by_agent: dict[str, list[dict]] = {}
    for t in tasks:
        if t["agent"]:
            by_agent.setdefault(t["agent"], []).append(t)
    for aid, entry in agents.items():
        mine = by_agent.get(aid, [])
        running = next((t for t in mine if t["stage"] == "running"), None)
        delivered = next((t for t in mine if t["stage"] == "delivered"), None)
        latest = mine[0] if mine else None
        # Blocked means the agent's most recent *finished* run escalated; a task
        # merely queued behind that failure does not make the agent healthy.
        finished = next((t for t in mine if t["stage"] in ("done", "escalated")), None)
        if running:
            entry["status"] = "working"
        elif finished and finished["stage"] == "escalated" and entry["status"] != "offline":
            done_at = parse_ts(finished["completed_at"]) or parse_ts(finished["created_at"])
            if done_at and (now - done_at).total_seconds() <= ESCALATION_BLOCK_SECONDS:
                entry["status"] = "blocked"
        current = running or delivered
        entry["current_task_id"] = current["id"] if current else None
        entry["model"] = (current or latest or {}).get("model")
        if entry["account"] is None and (current or latest):
            entry["account"] = (current or latest).get("account")
    return list(agents.values())


# --- Top level ---------------------------------------------------------------

def build_office_state(
    *,
    mc_tasks: Iterable[Any] = (),
    jobs: Iterable[Any] = (),
    routing_history: Iterable[dict] = (),
    memory_store: Any = None,
    handoff_manager: Any = None,
    accounts: Iterable[Any] = (),
    brain_root: Path | str | None = None,
    redactor: Any = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Assemble the office snapshot. Every source is optional and fails soft."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    redactor = redactor or _NullRedactor()

    root = Path(brain_root).expanduser() if brain_root else None
    resolved_root = None
    if root is not None:
        try:
            resolved_root = root.resolve(strict=True) if root.is_dir() else None
        except (OSError, RuntimeError):
            resolved_root = None
    swarm_records: list[tuple[str, dict]] = []
    skipped = 0
    budget = _Budget(MAX_TOTAL_BYTES)
    brain_swarm = bool(resolved_root and _within(resolved_root / "swarm" / "tasks", resolved_root)
                       and (resolved_root / "swarm" / "tasks").is_dir())
    if brain_swarm:
        try:
            swarm_records, skipped = read_swarm_records(resolved_root, budget)
        except Exception:
            # Headless agents write this pool with approval gates off: an
            # unforeseen read failure makes the swarm source unavailable for
            # this snapshot, never a 500 on every poll.
            swarm_records, skipped, brain_swarm = [], skipped + 1, False

    tasks: list[dict] = []
    seen: set[str] = set()
    for state, rec in swarm_records:
        try:
            t = normalize_swarm_task(state, rec, redactor)
        except Exception:
            skipped += 1
            continue
        if t["id"] and t["id"] not in seen:
            seen.add(t["id"])
            tasks.append(t)
    for normalize, items in ((normalize_mc_task, mc_tasks), (normalize_job, jobs)):
        for item in items or []:
            try:
                t = normalize(item, redactor)
            except Exception:
                continue
            if t["id"] and t["id"] not in seen:
                seen.add(t["id"])
                tasks.append(t)
    tasks.sort(key=_latest_ts, reverse=True)
    tasks = tasks[:MAX_TASKS]

    flows: list[dict] = []
    for t in tasks:
        flows.extend(task_flows(t, redactor))
    flows.extend(routing_flows(routing_history or [], redactor, {t["id"] for t in tasks}))

    memory = {"entries": 0, "recent": []}
    if memory_store is not None:
        try:
            memory["entries"] = int(memory_store.count())
        except Exception:
            pass
        try:
            recent = memory_store.list_all(limit=MAX_MEMORY_RECENT)
        except Exception:
            recent = []
        for m in recent:
            scope = getattr(getattr(m, "scope", None), "value", getattr(m, "scope", None))
            ts = iso(getattr(m, "created_at", None))
            agent = clean(getattr(m, "source_agent", None), redactor)
            memory["recent"].append({"ts": ts, "agent": agent, "scope": clean(scope, redactor, 40)})
            # Never the memory content: only who wrote which scope, and when.
            f = _flow(ts, "memory_write", getattr(m, "task_id", None), agent, "memory",
                      f"{scope or 'memory'} entry", redactor)
            if f:
                flows.append(f)

    handoff = read_brain_handoff(resolved_root, redactor) if resolved_root else None
    mc_record = None
    if handoff_manager is not None:
        try:
            mc_record = handoff_manager.get_current_record()
        except Exception:
            mc_record = None
    if mc_record is not None:
        f = _flow(getattr(mc_record, "created_at", None), "handoff", getattr(mc_record, "task_id", None),
                  getattr(mc_record, "agent_id", None) or "mission-control",
                  getattr(mc_record, "recommended_agent", None), getattr(mc_record, "next_action", None) or "handoff written",
                  redactor)
        if f:
            flows.append(f)
    if handoff is None and mc_record is not None:
        handoff = {
            "title": clean(getattr(mc_record, "task", None), redactor, TITLE_CHARS),
            "status": "done" if getattr(mc_record, "is_terminal", False) else "in-progress",
            "agent": clean(getattr(mc_record, "agent_id", None), redactor),
            "next": clean(getattr(mc_record, "next_action", None), redactor, DETAIL_CHARS),
            "updated_at": iso(getattr(mc_record, "created_at", None)),
            "source": "mission-control",
        }
    elif handoff is not None and handoff.get("updated_at"):
        f = _flow(handoff["updated_at"], "handoff", None, handoff.get("agent"), "handoff/current",
                  handoff.get("next") or handoff.get("title") or "baton updated", redactor)
        if f:
            flows.append(f)
    if handoff is None:
        handoff = {"title": None, "status": None, "agent": None, "next": None, "updated_at": None, "source": None}

    flows.sort(key=_flow_sort_key, reverse=True)
    agents = build_agents(accounts, tasks, brain_swarm, now, redactor)
    for t in tasks:
        t.pop("_error", None)
        t.pop("_staged_at", None)
        t.pop("_acknowledged", None)

    return {
        "generated_at": now.isoformat(),
        "sources": {
            "mission_control": True,
            "brain_swarm": brain_swarm,
            "brain_dir": str(resolved_root) if resolved_root else None,
            "skipped_files": skipped,
            "truncated_dirs": budget.truncated_dirs,
        },
        "agents": agents,
        "tasks": tasks,
        "flows": flows[:MAX_FLOWS],
        "memory": memory,
        "handoff": handoff,
    }


class SnapshotCache:
    """Serve one snapshot per TTL so the UI's 3s poll (x tabs) builds it at most once a second."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._key: Any = None
        self._at = 0.0

    def get(self, key: Any, build: Callable[[], dict]) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._value is not None and self._key == key and now - self._at < self.ttl:
                return self._value
            value = build()
            self._value, self._key, self._at = value, key, time.monotonic()
            return value

    def clear(self) -> None:
        with self._lock:
            self._value, self._key, self._at = None, None, 0.0
