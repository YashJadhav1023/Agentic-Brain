#!/usr/bin/env python3
"""Autonomous Self-Healing Sentinel for Shared Brain.

Features:
1. Picoschema Auto-Heal: Detects and auto-repairs missing/malformed fields in handoffs
2. Wikilink Self-Heal: Scans broken [[links]] and repairs them via fuzzy nearest-neighbor
3. Storage & DB Healer: Validates SQLite PRAGMA integrity and checkpoints WAL
4. Config Drift Auto-Heal: Enforces default_project=brain, hybrid search, and reranker
5. Daemon Watchdog: Probes search_sidecar (127.0.0.1:3334) and auto-spawns if degraded
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # package import for tests; plain import when run as a script
    from . import handoff_guard as _hg
except ImportError:
    import handoff_guard as _hg

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()


def resolve_data_dir() -> Path:
    """Resolve basic-memory's data dir exactly as basic_memory does.

    Mirrors basic_memory.config_models.resolve_data_dir: BASIC_MEMORY_CONFIG_DIR,
    then XDG_CONFIG_HOME/basic-memory, then ~/.basic-memory. The sentinel used to
    hardcode ~/.basic-memory, so on a desktop session (where XDG_CONFIG_HOME is
    exported) it healed the config of a database no agent was reading, and its
    database check silently matched nothing. See
    notes/two-basic-memory-homes-split-the-brain-index.
    """
    if explicit := os.environ.get("BASIC_MEMORY_CONFIG_DIR"):
        return Path(explicit)
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        return Path(xdg) / "basic-memory"
    return Path.home() / ".basic-memory"


DATA_DIR = resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
MEMORY_DB = DATA_DIR / "memory.db"
AUDIT_LOG = BRAIN_DIR / ".sentinel-audit.jsonl"

SIDECAR_HOST = "127.0.0.1"
SIDECAR_PORT = int(os.environ.get("BRAIN_SIDECAR_PORT", "3334"))
SIDECAR_SCRIPT = (Path(__file__).resolve().parent / "search_sidecar.py").resolve()


def _basic_memory_interpreter() -> Path:
    """Locate the interpreter that can import ``basic_memory``.

    The sidecar needs basic-memory's own environment, not whatever Python is
    running the sentinel. Resolve it from the ``bm`` launcher's shebang so the
    brain works on any machine and any install layout (uv tool, pipx, venv,
    system). ``BM_PYTHON`` overrides for unusual setups.

    Previously this was a hardcoded /home/<user>/.local/share/uv/... path that
    fell back to ``sys.executable``. The fallback does not crash, but it cannot
    import basic_memory either, so sidecar healing failed silently off the
    original machine.
    """
    override = os.environ.get("BM_PYTHON")
    if override and Path(override).exists():
        return Path(override)
    bm = shutil.which("bm") or os.environ.get("BM_BIN") or str(Path.home() / ".local" / "bin" / "bm")
    try:
        first_line = Path(bm).read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first_line.startswith("#!"):
            candidate = first_line[2:].strip().split()[0]
            if candidate and Path(candidate).exists():
                return Path(candidate)
    except Exception:
        pass
    return Path(sys.executable)


PYTHON_BIN = _basic_memory_interpreter()


def log_audit(action: str, details: dict):
    """Append a structured audit entry."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "details": details,
    }
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"Warning: Failed to write audit log: {e}", file=sys.stderr)


# ==============================================================================
# 1. Configuration Drift Auto-Healer
# ==============================================================================
def heal_configuration(dry_run: bool = False) -> list[str]:
    """Ensure basic-memory config has optimal, robust settings."""
    repairs = []
    if not CONFIG_PATH.exists():
        return repairs

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return [f"Failed to read {CONFIG_PATH}: {e}"]

    desired = {
        "default_project": "brain",
        "default_search_type": "hybrid",
        "write_note_overwrite_default": True,
        "reranker_enabled": True,
        "reranker_provider": "fastembed",
        "reranker_model": "jinaai/jina-reranker-v1-tiny-en",
        "reranker_candidates": 20,
        "reranker_max_document_chars": 2000,
        "semantic_search_enabled": True,
    }

    # Ensure projects.brain exists
    projects = cfg.get("projects", {})
    if "brain" not in projects:
        projects["brain"] = {
            "path": str(BRAIN_DIR),
            "mode": "local",
            "workspace_id": None,
            "local_sync_path": None,
            "bisync_initialized": False,
            "last_sync": None,
        }
        repairs.append("Added 'brain' project mapping to config")
        cfg["projects"] = projects

    modified = False
    for k, v in desired.items():
        if cfg.get(k) != v:
            old_val = cfg.get(k)
            repairs.append(f"Fixed config drift: {k}: {old_val} -> {v}")
            cfg[k] = v
            modified = True

    if modified and not dry_run:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        log_audit("config_healed", {"repairs": repairs})

    return repairs


# ==============================================================================
# 2. Broken Wikilink Self-Healer
# ==============================================================================
WIKILINK_RE = re.compile(r"\[\[([^\|\]]+)(?:\|([^\]]+))?\]\]")


def get_all_note_targets() -> dict[str, Path]:
    """Map valid note slugs, titles, and relative paths to actual Path."""
    targets: dict[str, Path] = {}
    if not BRAIN_DIR.exists():
        return targets

    for p in BRAIN_DIR.rglob("*.md"):
        if p.name.startswith(".") or ".git" in p.parts:
            continue
        rel = p.relative_to(BRAIN_DIR)
        rel_str = str(rel)
        rel_no_ext = str(rel.with_suffix(""))
        stem = p.stem

        targets[rel_str.lower()] = p
        targets[rel_no_ext.lower()] = p
        targets[stem.lower()] = p

        # Parse title from frontmatter
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.startswith("title:"):
                            t = line.split(":", 1)[1].strip().strip("\"'").lower()
                            if t:
                                targets[t] = p
        except Exception:
            pass

    return targets


def heal_wikilinks(dry_run: bool = False) -> list[str]:
    """Find broken [[links]] and auto-patch them with closest matching note."""
    repairs = []
    targets = get_all_note_targets()
    target_keys = list(targets.keys())

    for md_file in BRAIN_DIR.rglob("*.md"):
        if md_file.name.startswith(".") or ".git" in md_file.parts:
            continue

        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            continue

        modified = False

        def replace_link(match: re.Match) -> str:
            nonlocal modified
            raw_target = match.group(1).strip()
            alias = match.group(2)
            clean_target = raw_target.lower().strip()

            # Direct match
            if clean_target in targets or f"{clean_target}.md" in targets:
                return match.group(0)

            # Check without directory prefix
            basename = clean_target.split("/")[-1]
            if basename in targets:
                fixed = str(targets[basename].relative_to(BRAIN_DIR).with_suffix(""))
                repairs.append(
                    f"[{md_file.name}] Resolved [[{raw_target}]] -> [[{fixed}]]"
                )
                modified = True
                return f"[[{fixed}|{alias}]]" if alias else f"[[{fixed}]]"

            # Fuzzy match
            matches = difflib.get_close_matches(
                clean_target, target_keys, n=1, cutoff=0.6
            )
            if matches:
                best_match = matches[0]
                matched_path = targets[best_match]
                fixed = str(matched_path.relative_to(BRAIN_DIR).with_suffix(""))
                repairs.append(
                    f"[{md_file.name}] Fuzzy healed broken link [[{raw_target}]] -> [[{fixed}]]"
                )
                modified = True
                return f"[[{fixed}|{alias}]]" if alias else f"[[{fixed}]]"

            # No match found, leave as is
            return match.group(0)

        new_content = WIKILINK_RE.sub(replace_link, content)

        if modified and not dry_run:
            md_file.write_text(new_content, encoding="utf-8")
            log_audit(
                "wikilink_healed",
                {"file": str(md_file.relative_to(BRAIN_DIR))},
            )

    return repairs


# ==============================================================================
# 3. Picoschema & Handoff Auto-Healer
# ==============================================================================
def heal_handoff_note(
    handoff_path: Path, dry_run: bool = False
) -> tuple[bool, list[str]]:
    """Repair the live baton in place, without losing or inventing content.

    The previous healer rebuilt the note from a dict of observations, which
    dropped every prose line and section, every observation it did not know
    ([correction], [note], [archived], ...), all but the last [file]/[command]/
    [decision], and extra frontmatter such as the permalink. It also mapped any
    status containing "done" (e.g. "in-progress, not done") to done and filled
    missing fields with invented claims ("Task initialized and active").

    Now every repair is a local edit: frontmatter is prepended if missing, a
    status with trailing prose is split into [status] + [note], and a missing or
    empty required field gets a placeholder that says it was added by the healer
    and is unverified. Anything that needs judgement (duplicate or unknown
    status) is reported, not guessed.
    """
    repairs: list[str] = []
    if not handoff_path.exists():
        return False, ["Handoff note does not exist"]

    original = handoff_path.read_text(encoding="utf-8")
    content = original

    if not content.startswith("---"):
        title = handoff_path.stem
        content = f"---\ntitle: {title}\ntype: handoff\ntags:\n  - handoff\n  - active\n---\n\n" + content
        repairs.append(f"Added missing YAML frontmatter to {handoff_path.name}")

    parts = _hg.split_frontmatter(content)
    if parts is None:
        return False, repairs + ["needs attention: unclosed YAML frontmatter; not modified"]
    fm, body = parts

    statuses = _hg.observations(body, "status")
    if len(statuses) > 1:
        repairs.append(f"needs attention: [status] appears {len(statuses)} times; keep the latest by hand")
    elif len(statuses) == 1 and statuses[0] not in _hg.STATUS_VALUES:
        healed = _hg.fix_note(body)
        if _hg.observations(healed, "status") != statuses and healed != body:
            body = healed
            repairs.append(f"Split prose off [status] ('{statuses[0]}' -> '{_hg.observations(body, 'status')[0]}')")
        else:
            repairs.append(f"needs attention: unknown [status] '{statuses[0]}'; not guessed")

    placeholders = {
        "status": "in-progress",
        "agent": "unknown",
        "scope": "unknown",
        "done": "unknown",
        "next": "unknown",
        "blocker": "none",
    }
    missing: list[str] = []
    for tag, value in placeholders.items():
        values = _hg.observations(body, tag)
        if tag == "status":
            if not values:
                missing.append(f"- [status] {value}")
                missing.append("- [note] [status] was missing; added by brain heal (unverified)")
                repairs.append("Added missing [status] (in-progress, marked unverified)")
            continue
        marker = f"{value} (added by brain heal - unverified; the writing agent left [{tag}] empty)"
        if not values:
            missing.append(f"- [{tag}] {marker}")
            repairs.append(f"Added missing [{tag}] placeholder (marked unverified)")
        elif any(not v for v in values):
            body = re.sub(rf"^(-[ \t]*\[{tag}\])[ \t]*$", rf"\1 {marker}", body, flags=re.MULTILINE)
            repairs.append(f"Filled empty [{tag}] with a placeholder (marked unverified)")

    if missing:
        insert = "\n".join(missing) + "\n"
        obs = re.search(r"^## Observations[ \t]*\n", body, re.MULTILINE)
        if obs:
            # After the last observation line of the Observations section.
            section_end = re.search(r"^## ", body[obs.end():], re.MULTILINE)
            limit = obs.end() + (section_end.start() if section_end else len(body) - obs.end())
            last = None
            for m in re.finditer(r"^-[ \t]*\[[^\]]+\].*\n?", body[obs.end():limit], re.MULTILINE):
                last = m
            pos = obs.end() + (last.end() if last else 0)
            if pos > 0 and not body[:pos].endswith("\n"):
                insert = "\n" + insert
            body = body[:pos] + insert + body[pos:]
        else:
            relations = re.search(r"^## Relations", body, re.MULTILINE)
            block = "## Observations\n" + insert + "\n"
            if relations:
                body = body[: relations.start()] + block + body[relations.start():]
            else:
                body = body.rstrip("\n") + "\n\n" + block
        repairs.append("Inserted missing observations without rewriting the rest of the note")

    content = f"---{fm}---{body}"
    if content != original and not dry_run:
        _hg.atomic_write(handoff_path, content)
        log_audit(
            "handoff_healed",
            {"file": handoff_path.name, "repairs": repairs},
        )

    return True, repairs


# ==============================================================================
# 4. Storage & Database Healer
# ==============================================================================
def heal_database(dry_run: bool = False) -> list[str]:
    """Run SQLite integrity check and WAL checkpoint."""
    repairs = []

    db_candidates = [
        MEMORY_DB,
        # Legacy layouts, kept so an older install still gets checked.
        DATA_DIR / "default.db",
        BRAIN_DIR / ".basic-memory" / "knowledge.db",
    ]

    for db_path in db_candidates:
        if not db_path.exists():
            continue

        try:
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()

            cur.execute("PRAGMA integrity_check;")
            row = cur.fetchone()
            if row and row[0] != "ok":
                repairs.append(f"DB Warning ({db_path.name}): {row[0]}")
            else:
                repairs.append(f"DB Integrity OK: {db_path.name}")

            if not dry_run:
                cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                cur.execute("PRAGMA optimize;")
                repairs.append(f"Checkpoint and vacuum optimized: {db_path.name}")

            conn.close()
        except Exception as e:
            repairs.append(f"DB Check error on {db_path.name}: {e}")

    return repairs


# ==============================================================================
# 4b. Index Freshness Healer
# ==============================================================================


def _bm_binary() -> Path | None:
    """Locate the basic-memory CLI."""
    if explicit := os.environ.get("BM_BIN"):
        return Path(explicit)
    found = shutil.which("basic-memory")
    if found:
        return Path(found)
    candidate = Path.home() / ".local" / "bin" / "basic-memory"
    return candidate if candidate.exists() else None


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block.

    note_content.markdown_content stores the body without frontmatter, so a
    comparison against the raw file has to strip it first.
    """
    if not text.startswith("---"):
        return text.strip()
    end = text.find("\n---", 3)
    if end == -1:
        return text.strip()
    return text[end + 4 :].strip()


def find_stale_index_rows() -> tuple[list[str], list[str]]:
    """Find index rows whose cached body no longer matches the file on disk.

    read_note and read_content serve note_content.markdown_content, not the file,
    so a row left behind by an unobserved disk write is indistinguishable from the
    truth to any agent. This is the failure that silently breaks the handoff
    protocol, and nothing else in this suite looks for it.

    Staleness requires BOTH a newer mtime and genuinely different content.
    basic-memory tracks notes by checksum, so `reindex` deliberately skips a file
    whose bytes did not change; flagging on mtime alone would report a permanent
    false positive after any touch, rsync or git checkout.

    Returns (stale, orphaned) as lists of human-readable descriptions.
    """
    stale: list[str] = []
    orphaned: list[str] = []
    if not MEMORY_DB.exists():
        return stale, orphaned

    try:
        # mode=ro still applies the WAL, so this sees committed writes without
        # taking a write lock on a database the MCP servers hold open.
        conn = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
        rows = conn.execute(
            """
            SELECT p.path, n.file_path, n.file_updated_at, e.permalink, n.markdown_content
            FROM note_content n
            JOIN entity e ON e.id = n.entity_id
            JOIN project p ON p.id = n.project_id
            """
        ).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001 - a broken index must not crash the audit
        return [f"Index freshness check failed: {e}"], orphaned

    for project_path, file_path, file_updated_at, permalink, cached in rows:
        if not project_path or not file_path:
            continue
        full = Path(project_path) / file_path
        if not full.exists():
            orphaned.append(f"{permalink} -> {file_path} (file missing on disk)")
            continue

        try:
            on_disk_text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if _strip_frontmatter(on_disk_text) == _strip_frontmatter(cached or ""):
            continue  # bytes agree; mtime drift is irrelevant

        # Content differs. Report the timestamps so the drift is quantified.
        on_disk_mtime = datetime.fromtimestamp(full.stat().st_mtime)
        try:
            indexed = datetime.fromisoformat(str(file_updated_at))
        except (ValueError, TypeError):
            stale.append(f"{permalink}: content differs from disk (no usable index timestamp)")
            continue
        drift_hours = (on_disk_mtime - indexed).total_seconds() / 3600
        stale.append(
            f"{permalink}: content differs from disk — index cached "
            f"{indexed:%Y-%m-%d %H:%M:%S}, file changed "
            f"{on_disk_mtime:%Y-%m-%d %H:%M:%S} ({drift_hours:+.1f}h)"
        )

    return stale, orphaned


def heal_index_freshness(dry_run: bool = False) -> tuple[list[str], list[str]]:
    """Reindex when the index has fallen behind the files on disk.

    Returns (repairs, warnings). A warning survives the repair only when the
    reindex did not actually resolve the divergence.
    """
    repairs: list[str] = []
    warnings: list[str] = []

    stale, orphaned = find_stale_index_rows()
    for orphan in orphaned:
        warnings.append(f"Orphaned index row: {orphan}")

    if not stale:
        if MEMORY_DB.exists():
            repairs.append(f"Index fresh against disk: {MEMORY_DB.name}")
        return repairs, warnings

    for entry in stale[:10]:
        repairs.append(f"STALE index row: {entry}")
    if len(stale) > 10:
        repairs.append(f"...and {len(stale) - 10} more stale rows")

    if dry_run:
        repairs.append(f"Would run 'basic-memory reindex' to repair {len(stale)} stale row(s)")
        warnings.append(f"Index is stale for {len(stale)} note(s); agents may read old content")
        return repairs, warnings

    bm = _bm_binary()
    if bm is None:
        warnings.append("Cannot repair stale index: basic-memory CLI not found")
        return repairs, warnings

    try:
        proc = subprocess.run(
            [str(bm), "reindex"],
            capture_output=True,
            text=True,
            timeout=900,
            env={**os.environ, "BASIC_MEMORY_CONFIG_DIR": str(DATA_DIR)},
        )
    except subprocess.TimeoutExpired:
        warnings.append("Index reindex timed out after 900s")
        return repairs, warnings

    if proc.returncode != 0:
        warnings.append(f"Index reindex failed (exit {proc.returncode})")
        return repairs, warnings

    remaining, _ = find_stale_index_rows()
    if remaining:
        warnings.append(
            f"Reindex ran but {len(remaining)} row(s) are still stale; "
            "the file may have been written by a process using a different data dir"
        )
    else:
        repairs.append(f"Reindexed: {len(stale)} stale row(s) now match disk")

    return repairs, warnings


# ==============================================================================
# 5. Daemon Watchdog & Auto-Spawner
# ==============================================================================
def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def probe_sidecar_health() -> bool:
    """Return True if sidecar returns 200 OK from /healthz."""
    try:
        url = f"http://{SIDECAR_HOST}:{SIDECAR_PORT}/healthz"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("ready") is True or data.get("status") == "ready"
    except Exception:
        pass
    return False


def _stale_sidecar_pids(port: int) -> list[int]:
    """PIDs listening on `port` that are this user's search_sidecar.py processes."""
    try:
        out = subprocess.run(["fuser", f"{port}/tcp"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return []
    pids = []
    for tok in out.split():
        if not tok.isdigit():
            continue
        pid = int(tok)
        try:
            proc = Path(f"/proc/{pid}")
            if proc.stat().st_uid != os.getuid():
                continue
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.endswith(SIDECAR_SCRIPT.name.encode()) for a in argv):
            pids.append(pid)
    return pids


def heal_sidecar(dry_run: bool = False) -> list[str]:
    """Ensure search_sidecar is healthy and running; restart if degraded."""
    repairs = []

    if probe_sidecar_health():
        return ["Sidecar active and healthy (127.0.0.1:3334)"]

    repairs.append("Sidecar inactive or unresponsive")

    if dry_run:
        repairs.append("Dry run: Would respawn search_sidecar.py")
        return repairs

    if is_port_in_use(SIDECAR_PORT):
        # Only ever stop a process that is provably our own stale sidecar. The
        # port may belong to something unrelated, and `fuser -k` would kill it.
        stale = _stale_sidecar_pids(SIDECAR_PORT)
        if not stale:
            repairs.append(
                f"Port {SIDECAR_PORT} is held by a process that is not search_sidecar.py; "
                "leaving it alone and not respawning"
            )
            log_audit("sidecar_healed", {"repairs": repairs})
            return repairs
        repairs.append(f"Port {SIDECAR_PORT} occupied by stale sidecar pid(s) {stale}; stopping them")
        for pid in stale:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(1)

    if SIDECAR_SCRIPT.exists():
        cmd = [str(PYTHON_BIN), str(SIDECAR_SCRIPT)]
        log_file = open("/tmp/brain_search_sidecar.log", "a")
        subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        repairs.append("Spawned persistent search_sidecar daemon")

        for _ in range(12):
            time.sleep(0.5)
            if probe_sidecar_health():
                repairs.append("Sidecar warmup complete; health status READY")
                break
    else:
        repairs.append(f"Sidecar script not found at {SIDECAR_SCRIPT}")

    log_audit("sidecar_healed", {"repairs": repairs})
    return repairs


# ==============================================================================
# Full Self-Healing Suite Orchestrator
# ==============================================================================
def run_self_healing_suite(dry_run: bool = False) -> dict:
    """Executes complete self-healing suite and returns comprehensive health status."""
    start_time = time.time()
    all_repairs = []
    warnings = []

    # 1. Config Drift
    cfg_repairs = heal_configuration(dry_run=dry_run)
    all_repairs.extend(cfg_repairs)

    # 2. Handoff Schema
    handoff_path = BRAIN_DIR / "handoff" / "current.md"
    if handoff_path.exists():
        ok, h_repairs = heal_handoff_note(handoff_path, dry_run=dry_run)
        all_repairs.extend(h_repairs)

    # 3. Wikilinks
    w_repairs = heal_wikilinks(dry_run=dry_run)
    all_repairs.extend(w_repairs)

    # 4. Database & Storage
    db_repairs = heal_database(dry_run=dry_run)
    all_repairs.extend(db_repairs)

    # 4b. Index Freshness — the index serving read_note must match the files on
    # disk, or every agent resumes from a stale baton while the score reads 100%.
    idx_repairs, idx_warnings = heal_index_freshness(dry_run=dry_run)
    all_repairs.extend(idx_repairs)
    warnings.extend(idx_warnings)

    # 5. Sidecar Watchdog
    sidecar_repairs = heal_sidecar(dry_run=dry_run)
    all_repairs.extend(sidecar_repairs)

    sidecar_healthy = probe_sidecar_health()
    score = 100
    if not sidecar_healthy:
        score -= 20
        warnings.append("Neural search sidecar is degraded")

    # A stale index is the most damaging failure in this suite: it is invisible to
    # callers and it breaks the resume contract. Weight it above the sidecar.
    index_stale, index_orphans = find_stale_index_rows()
    if index_stale:
        score -= 40
    if index_orphans:
        score -= 5

    duration = round(time.time() - start_time, 3)

    return {
        "status": "healthy" if score >= 90 else "degraded",
        "health_score": max(score, 0),
        "dry_run": dry_run,
        "repairs_count": len(all_repairs),
        "repairs": all_repairs,
        "warnings": warnings,
        "sidecar_healthy": sidecar_healthy,
        "data_dir": str(DATA_DIR),
        "index_stale_count": len(index_stale),
        "index_orphan_count": len(index_orphans),
        "duration_seconds": duration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Autonomous Self-Healing Sentinel for Shared Brain"
    )
    parser.add_argument(
        "--heal",
        action="store_true",
        default=True,
        help="Execute automated repairs",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate repairs without modifying files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON health report",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Run continuously in background monitor mode",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Watch interval in seconds (default 300)",
    )

    args = parser.parse_args()

    if args.watch:
        print(f"Sentinel watchdog running (polling every {args.interval}s)...")
        while True:
            try:
                res = run_self_healing_suite(dry_run=False)
                if res["repairs_count"] > 0:
                    print(
                        f"[{datetime.now().strftime('%H:%M:%S')}] Sentinel auto-repaired {res['repairs_count']} items (Score: {res['health_score']}%)"
                    )
            except Exception as e:
                print(f"Sentinel error: {e}", file=sys.stderr)
            time.sleep(args.interval)
        return

    result = run_self_healing_suite(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    mode_str = "DRY RUN (Simulated)" if args.dry_run else "LIVE AUTO-REPAIR"
    print("==================================================================")
    print(f"  AUTONOMOUS SENTINEL HEALTH AUDIT & HEALING [{mode_str}]")
    print("==================================================================")
    print(f"Health Score: {result['health_score']}% [{result['status'].upper()}]")
    print(f"Sidecar Neural Engine: {'ONLINE' if result['sidecar_healthy'] else 'OFFLINE'}")
    print(f"Data Dir: {result['data_dir']}")
    stale_n = result["index_stale_count"]
    orphan_n = result["index_orphan_count"]
    print(
        "Index vs Disk: "
        + ("IN SYNC" if stale_n == 0 else f"STALE ({stale_n} note(s) behind disk)")
        + (f", {orphan_n} orphaned row(s)" if orphan_n else "")
    )
    print(f"Scan Duration: {result['duration_seconds']}s")
    print("------------------------------------------------------------------")
    print("Actions & Repairs Applied:")
    for r in result["repairs"]:
        print(f"  ✓ {r}")
    if result["warnings"]:
        print("------------------------------------------------------------------")
        print("Active Warnings:")
        for w in result["warnings"]:
            print(f"  ! {w}")
    print("==================================================================")


if __name__ == "__main__":
    main()
