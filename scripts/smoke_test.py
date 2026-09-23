#!/usr/bin/env python3
"""Hermetic end-to-end smoke test for Agentic Brain.

Exercises the Mission Control HTTP API (every route, happy path plus one bad
input each, including the SSE stream), every ``scripts/brain.py`` subcommand,
the shared-memory store (write/read/search round trip and concurrent writes),
the task lifecycle (submit -> status -> cancel), jobs, handoffs and the account
registry.

Hermetic by construction:

* The repository's tracked files are copied into a fresh temporary directory
  and the test runs against that copy, so nothing is written to your checkout
  (``runtime/``, ``memory/store/``, ``tasks/``, ``handoffs/`` all live in the
  copy).
* ``HOME``, ``BRAIN_DIR``, ``XDG_*`` and the providers config all point into
  the temporary directory, so ``~/agentic-brain``, ``~/.gemini`` and your real
  ``config/providers.json`` are never read or written.
* ``agy``, ``kiro-cli``, ``cline``, ``openhands`` and ``secret-tool`` are
  shadowed by shims on ``PATH`` that refuse to run and record the attempt, so
  no real agent is ever executed and the OS keyring is never touched.
  Provider API keys are removed from the environment.
* The dashboard binds 127.0.0.1 on an ephemeral port and is stopped at exit.

Standard library only. Exits 0 when every check passes, 1 otherwise.

Usage::

    python3 scripts/smoke_test.py            # run everything
    python3 scripts/smoke_test.py --verbose  # print evidence for passes too
    python3 scripts/smoke_test.py --keep     # keep the temp dir for inspection
    python3 scripts/smoke_test.py --only cli # only checks whose group matches
    python3 scripts/smoke_test.py --json     # machine-readable results
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Binaries that must never really run during a smoke test.
SHIMMED_BINARIES = ("agy", "kiro-cli", "cline", "openhands", "gemini", "secret-tool")

#: Environment variables that could reach a real provider or the user's state.
SCRUBBED_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY", "GROQ_API_KEY", "MISSION_CONTROL_AUTH_TOKEN",
    "DBUS_SESSION_BUS_ADDRESS", "BRAIN_PROTECTED_PIDS", "BRAIN_HOST",
    "ANTIGRAVITY_OAUTH_CLIENT_ID", "ANTIGRAVITY_OAUTH_CLIENT_SECRET",
)


# ── Result bookkeeping ──────────────────────────────────────────────────────

@dataclass
class Result:
    group: str
    feature: str
    how: str
    ok: bool
    evidence: str


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)
    verbose: bool = False

    def add(self, group: str, feature: str, how: str, ok: bool, evidence: str) -> bool:
        evidence = " ".join(str(evidence).split())[:300]
        self.results.append(Result(group, feature, how, bool(ok), evidence))
        mark = "PASS" if ok else "FAIL"
        if self.verbose or not ok:
            print(f"  [{mark}] {group}: {feature} -- {evidence}", flush=True)
        else:
            print(f"  [{mark}] {group}: {feature}", flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[Result]:
        return [r for r in self.results if not r.ok]


# ── Hermetic sandbox ────────────────────────────────────────────────────────

class Sandbox:
    """A throwaway copy of the repository plus an isolated HOME."""

    def __init__(self, keep: bool = False) -> None:
        self.keep = keep
        self.tmp = Path(tempfile.mkdtemp(prefix="brain-smoke-"))
        self.repo = self.tmp / "repo"
        self.home = self.tmp / "home"
        self.brain = self.tmp / "agentic-brain"
        self.shims = self.tmp / "shims"
        self.shim_log = self.tmp / "shim_calls.log"
        for d in (self.home, self.brain, self.shims):
            d.mkdir(parents=True)
        self._copy_repo()
        self._write_shims()
        self.env = self._build_env()
        self._git_init()

    def _tracked_files(self) -> list[str]:
        try:
            out = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
                capture_output=True, check=True,
            ).stdout.decode("utf-8")
            return [f for f in out.split("\0") if f]
        except Exception:
            skip = {".git", "runtime", "__pycache__", "node_modules", ".venv", "venv"}
            files = []
            for root, dirs, names in os.walk(REPO_ROOT):
                dirs[:] = [d for d in dirs if d not in skip]
                for n in names:
                    files.append(str(Path(root, n).relative_to(REPO_ROOT)))
            return files

    def _copy_repo(self) -> None:
        for rel in self._tracked_files():
            src = REPO_ROOT / rel
            if not src.is_file():
                continue  # deleted in the working tree
            dst = self.repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        # Never carry over handoff state that may exist in the source checkout.
        for stale in ("handoffs/current.json", "handoffs/current.md"):
            (self.repo / stale).unlink(missing_ok=True)

    def _write_shims(self) -> None:
        """Fake agent binaries.

        A shim answers a bare version/help probe (so health checks and routing
        see an "installed" agent) and refuses everything else with exit 127.
        Every invocation is logged, so the report can prove that no execution
        ever reached a real agent. ``secret-tool`` always fails with "Invalid
        bus", which makes the keyring store report itself unavailable.
        """
        for name in SHIMMED_BINARIES:
            probe_ok = name != "secret-tool"
            shim = self.shims / name
            shim.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"{name} $1 $2 (argc=$#)\" >> '{self.shim_log}'\n"
                + (
                    'case "$1" in --version|-v|version|--help|-h) '
                    f'echo "{name} 0.0.0-smoke-shim"; exit 0;; esac\n'
                    if probe_ok else ""
                )
                + "echo 'smoke-test shim: Invalid bus; real binary blocked' >&2\n"
                "exit 127\n",
                encoding="utf-8",
            )
            shim.chmod(0o755)
        # config/providers.json addresses the CLIs by ~-relative path; with HOME
        # redirected these resolve into the sandbox, so link the shims there.
        for rel, name in ((".gemini/bin/agy", "agy"), (".local/bin/kiro-cli", "kiro-cli"),
                          (".local/bin/cline", "cline")):
            target = self.home / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(self.shims / name)
        # Empty per-account profile directories, so shallow health checks see
        # "installed" agents and routing has candidates. Deep health checks and
        # executions still hit the shims and are refused.
        try:
            cfg = json.loads((self.repo / "config" / "providers.json").read_text("utf-8"))
        except (OSError, ValueError):
            cfg = {}
        for prov in (cfg.get("providers") or {}).values():
            for acct in (prov.get("accounts") or {}).values():
                ex = acct.get("execution") or {}
                for key in ("app_data_dir", "data_dir", "config_dir"):
                    val = ex.get(key) or acct.get(key)
                    if not val:
                        continue
                    if val.startswith("~"):
                        path = self.home / val[2:]
                    elif not os.path.isabs(val):
                        path = self.home / ".gemini" / val
                    else:
                        continue  # never create absolute paths outside the sandbox
                    path.mkdir(parents=True, exist_ok=True)

    def _build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
        real_home = str(Path.home())
        # Drop user-local bin dirs where the real agent CLIs live, then put the
        # shims first so any lookup by name hits a shim.
        path_parts = [
            p for p in env.get("PATH", "/usr/bin:/bin").split(os.pathsep)
            if p and not p.startswith(real_home)
        ]
        env.update({
            "PATH": os.pathsep.join([str(self.shims)] + path_parts),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "BRAIN_DIR": str(self.brain),
            "BRAIN_WORKSPACE_ROOT": str(self.repo),
            "BRAIN_REPO_ROOT": str(self.repo),
            "BRAIN_SIBLING_WORKSPACE": str(self.repo),
            "BRAIN_PROVIDERS_CONFIG": str(self.repo / "config" / "providers.json"),
            "BRAIN_SANDBOX_ROOT": str(self.tmp / "sandboxes"),
            "AGENTIC_SANDBOX_ROOT": str(self.tmp / "sandboxes"),
            "BRAIN_CLINE_CONFIG_DIR": str(self.home / ".cline"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent-smoke-test-bus",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "smoke", "GIT_AUTHOR_EMAIL": "smoke@localhost",
            "GIT_COMMITTER_NAME": "smoke", "GIT_COMMITTER_EMAIL": "smoke@localhost",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def _git_init(self) -> None:
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "smoke baseline"],
        ):
            subprocess.run(cmd, cwd=self.repo, env=self.env, check=True,
                           capture_output=True)

    def shim_calls(self) -> list[str]:
        if not self.shim_log.is_file():
            return []
        return [l for l in self.shim_log.read_text().splitlines() if l.strip()]

    def cleanup(self) -> None:
        if self.keep:
            print(f"Sandbox kept at {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)


# ── CLI runner ──────────────────────────────────────────────────────────────

@dataclass
class CliResult:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return (self.out or "") + (self.err or "")

    @property
    def traceback(self) -> bool:
        return "Traceback (most recent call last)" in self.text


def run_cli(sb: Sandbox, args: list[str], timeout: float = 120.0,
            stdin: str | None = None) -> CliResult:
    try:
        p = subprocess.run(
            [sys.executable, str(sb.repo / "scripts" / "brain.py"), *args],
            cwd=sb.repo, env=sb.env, capture_output=True, text=True,
            timeout=timeout, input=stdin if stdin is not None else "",
        )
        return CliResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as exc:
        return CliResult(-999, str(exc.stdout or ""), f"TIMEOUT after {timeout}s")


# ── Dashboard client ────────────────────────────────────────────────────────

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Dashboard:
    def __init__(self, sb: Sandbox) -> None:
        self.sb = sb
        self.port = free_port()
        self.token: str | None = None
        self.log = sb.tmp / "dashboard.log"
        env = dict(sb.env, BRAIN_PORT=str(self.port))
        self._log_fh = open(self.log, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(sb.repo / "ui" / "dashboard" / "dashboard.py")],
            cwd=sb.repo / "ui" / "dashboard", env=env,
            stdout=self._log_fh, stderr=subprocess.STDOUT,
        )

    def wait_ready(self, timeout: float = 60.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                st, _, _ = self.request("GET", "/api/token", auth=False, timeout=5)
                if st == 200:
                    return True
            except OSError:
                pass
            time.sleep(0.5)
        return False

    def request(self, method: str, path: str, body: Any = None, auth: bool = True,
                timeout: float = 30.0, raw_body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, Any, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        hdrs = dict(headers or {})
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if auth and self.token:
            hdrs["Authorization"] = f"Bearer {self.token}"
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                parsed: Any = json.loads(raw.decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                parsed = raw.decode("utf-8", "replace")
            return resp.status, parsed, dict(resp.getheaders())
        finally:
            conn.close()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self._log_fh.close()


def short(obj: Any, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s[:n]


# ── Checks: CLI ─────────────────────────────────────────────────────────────

#: Every subcommand path exposed by scripts/brain.py, for --help coverage.
CLI_HELP_PATHS: list[list[str]] = [
    [], ["external"], ["external", "list"], ["external", "enable"],
    ["external", "disable"], ["external", "status"],
    ["agents"], ["health"], ["route"], ["metrics"], ["audit"], ["plan"],
    ["execute"], ["run"], ["approve"], ["reject"], ["continue"], ["status"],
    ["sessions"], ["worktree"], ["worktree", "status"], ["worktree", "diff"],
    ["worktree", "approve"], ["worktree", "reject"], ["worktree", "recover"],
    ["worktree", "cleanup"], ["benchmark"],
    ["accounts"], ["accounts", "list"], ["accounts", "add"], ["accounts", "inspect"],
    ["accounts", "enable"], ["accounts", "disable"], ["accounts", "remove"],
    ["accounts", "health"], ["accounts", "rotate"], ["accounts", "test"],
    ["credential"], ["credential", "set"], ["credential", "exists"],
    ["credential", "delete"], ["credential", "rotate"],
    ["providers"], ["providers", "list"], ["providers", "discover"],
    ["providers", "add"], ["providers", "health"], ["providers", "inspect"],
    ["models"], ["models", "list"], ["models", "discover"], ["models", "health"],
    ["job"], ["jobs"], ["job", "submit"], ["job", "list"], ["job", "inspect"],
    ["routing"], ["routing", "history"], ["routing", "inspect"],
    ["usage"], ["usage", "summary"], ["usage", "provider"], ["usage", "account"],
    ["usage", "model"], ["cost"], ["cost", "summary"],
    ["quota"], ["quota", "list"], ["quota", "set"], ["quota", "reset"],
    ["dashboard"], ["maintenance"], ["maintenance", "rotate-logs"],
    ["mcp"], ["mcp", "discover"], ["mcp", "list"], ["mcp", "inspect"],
    ["mcp", "health"], ["mcp", "enable"], ["mcp", "disable"],
    ["steering"], ["steering", "discover"], ["steering", "list"],
    ["steering", "inspect"], ["docs"], ["docs", "list"],
    ["knowledge"], ["knowledge", "discover"], ["knowledge", "list"],
    ["knowledge", "search"], ["knowledge", "inspect"],
    ["tools"], ["tools", "discover"], ["tools", "list"], ["tools", "search"],
    ["tools", "inspect"], ["tools", "health"],
    ["resources"], ["resources", "discover"], ["resources", "list"],
    ["resources", "search"], ["resources", "inspect"], ["resources", "health"],
    ["context"], ["context", "preview"], ["context", "explain"],
    ["context", "inspect"], ["context", "search"], ["validate"],
]


def check_cli_help(sb: Sandbox, rep: Report) -> None:
    bad = []
    lock = threading.Lock()

    def one(path: list[str]) -> None:
        r = run_cli(sb, [*path, "--help"], timeout=60)
        if r.rc != 0 or "usage:" not in r.out:
            with lock:
                bad.append(f"{' '.join(path) or '<root>'} rc={r.rc} {r.err.strip()[-120:]}")

    threads = [threading.Thread(target=one, args=(p,)) for p in CLI_HELP_PATHS]
    for i in range(0, len(threads), 8):
        batch = threads[i:i + 8]
        for t in batch:
            t.start()
        for t in batch:
            t.join()
    rep.add("cli", f"--help for all {len(CLI_HELP_PATHS)} command paths",
            "brain.py <cmd> [sub] --help", not bad,
            "all exit 0 with usage" if not bad else "; ".join(bad))


def cli_case(sb: Sandbox, rep: Report, feature: str, args: list[str],
             expect_ok: bool | None = True, must_contain: str | None = None,
             json_out: bool = False, timeout: float = 120.0,
             stdin: str | None = None) -> CliResult:
    r = run_cli(sb, args, timeout=timeout, stdin=stdin)
    how = "brain.py " + " ".join(args)
    if expect_ok is None:
        # Informational command: either exit code is fine, a crash is not.
        ok = r.rc in (0, 1) and not r.traceback and bool(r.out.strip())
        ev = f"rc={r.rc} " + r.text.strip()[:200]
    elif expect_ok:
        ok = r.rc == 0 and not r.traceback
        if ok and must_contain is not None:
            ok = must_contain.lower() in r.text.lower()
        if ok and json_out:
            try:
                json.loads(r.out)
            except ValueError:
                ok = False
        ev = f"rc={r.rc} " + (r.out.strip()[:160] if ok else r.text.strip()[-240:])
    else:
        # A bad input must be rejected cleanly: non-zero exit, no traceback.
        ok = r.rc not in (0, -999) and not r.traceback
        ev = f"rc={r.rc} " + r.text.strip()[-200:]
    rep.add("cli", feature, how, ok, ev)
    return r


def check_cli(sb: Sandbox, rep: Report) -> None:
    check_cli_help(sb, rep)
    c = lambda *a, **k: cli_case(sb, rep, *a, **k)  # noqa: E731

    c("agents", ["agents"])
    # `health` exits 1 when any account is unhealthy (by design); it must
    # still report cleanly rather than crash.
    c("health", ["health"], expect_ok=None)
    c("health --deep", ["health", "--deep"], expect_ok=None)
    c("route", ["route", "Design event streaming architecture"])
    c("route explain (README form)", ["route", "explain", "Refactor auth tokens across services"])
    c("route --explain --json", ["route", "--explain", "--json", "Refactor auth tokens"], json_out=True)
    c("route: missing instruction rejected", ["route"], expect_ok=False)
    c("metrics", ["metrics"])
    c("audit", ["audit", "--limit", "5"])
    c("audit: bad --limit rejected", ["audit", "--limit", "notanint"], expect_ok=False)
    c("status", ["status"])
    c("sessions", ["sessions"])
    c("continue --dry-run (no handoff)", ["continue", "--dry-run"])
    # Nothing to continue: must report cleanly, never crash.
    c("continue with empty state reports cleanly", ["continue"], expect_ok=None)
    c("dashboard: non-loopback host rejected", ["dashboard", "--host", "0.0.0.0"], expect_ok=False)
    c("reject: unknown task rejected", ["reject", "task-doesnotexist"], expect_ok=False)
    c("approve: unknown task rejected", ["approve", "task-doesnotexist"], expect_ok=False)
    c("unknown command rejected", ["definitely-not-a-command"], expect_ok=False)

    c("worktree status", ["worktree", "status"])
    c("worktree diff: unknown task rejected", ["worktree", "diff", "task-doesnotexist"], expect_ok=False)
    c("worktree cleanup", ["worktree", "cleanup"])

    c("accounts list", ["accounts", "list"])
    c("accounts list --json", ["accounts", "list", "--json"], json_out=True)
    c("accounts inspect: unknown rejected", ["accounts", "inspect", "no-such-account"], expect_ok=False)
    c("accounts health", ["accounts", "health"])
    c("providers list", ["providers", "list"])
    c("providers discover", ["providers", "discover"])
    c("providers inspect: unknown rejected", ["providers", "inspect", "no-such-provider"], expect_ok=False)
    c("models list", ["models", "list"])
    c("credential exists (unset)", ["credential", "exists", "smoke-test-cred"])
    c("jobs list", ["jobs", "list"])
    c("job inspect: unknown rejected", ["job", "inspect", "job-doesnotexist"], expect_ok=False)
    c("routing history", ["routing", "history"])
    c("routing inspect: unknown rejected", ["routing", "inspect", "job-doesnotexist"], expect_ok=False)
    c("usage summary", ["usage", "summary"])
    c("cost summary", ["cost", "summary"])
    c("quota list", ["quota", "list"])
    c("maintenance rotate-logs", ["maintenance", "rotate-logs"])
    c("mcp list", ["mcp", "list"])
    c("mcp inspect: unknown rejected", ["mcp", "inspect", "no-such-server"], expect_ok=False)
    c("steering list", ["steering", "list"])
    c("knowledge list", ["knowledge", "list"])
    c("docs list", ["docs", "list"])
    c("knowledge search", ["knowledge", "search", "architecture"])
    c("tools list", ["tools", "list"])
    c("tools search", ["tools", "search", "read"])
    c("resources list", ["resources", "list"])
    c("resources health", ["resources", "health"])
    c("context preview", ["context", "preview", "Audit repository security"])
    c("context inspect: unknown rejected", ["context", "inspect", "ctx-doesnotexist"], expect_ok=False)
    c("external status", ["external", "status"])
    c("external list", ["external", "list"])

    # Task lifecycle through the CLI: plan -> status -> reject (cancel).
    r = c("plan (enqueue task)", ["plan", "Write a short summary of the README"])
    task_id = _find_task_id(r.text)
    if task_id:
        st = c("status shows planned task", ["status"], must_contain=task_id)
        c("reject planned task", ["reject", task_id])
    else:
        rep.add("cli", "plan -> status -> reject", "brain.py plan", False,
                "could not find a task id in plan output: " + r.text[-200:])

    # Plan -> execute: runs the swarm up to the agent CLI, which is a shim.
    r = c("plan (second task, for execute)", ["plan", "Run the unit tests and report"])
    c("execute (reaches shimmed agent CLI, no real execution)", ["execute"], expect_ok=None,
      timeout=300)
    c("plan list", ["plan", "list"])
    c("validate --json (full-system gate)", ["validate", "--json"], expect_ok=None, timeout=300)
    c("benchmark (tests A-G)", ["benchmark"], expect_ok=None, timeout=300)

    calls = sb.shim_calls()
    rep.add("safety", "no real agent CLI or keyring invoked by CLI checks",
            "shim call log", True,
            f"{len(calls)} shim invocations (all blocked): {calls[:5]}")


def _find_task_id(text: str) -> str | None:
    import re
    m = re.search(r"\btask-[0-9a-f]{6,}\b", text)
    return m.group(0) if m else None


# ── Checks: memory (in-process against the sandbox copy) ────────────────────

MEMORY_PROBE = r"""
import json, sys, threading
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from memory.store.memory_store import MemoryStore, MemoryScope
from memory.retrieval.retriever import MemoryRetriever

db = Path(sys.argv[2])
store = MemoryStore(db_path=db)
out = {}
e = store.add(content="smoke zebra-quartz marker", scope=MemoryScope.PROJECT,
              source_agent="smoke", importance=4, tags=["smoke"])
out["added_id"] = e.to_dict().get("id") or e.to_dict().get("memory_id")
hits = store.query_memories(search="zebra-quartz", limit=10)
out["query_hits"] = len(hits)
res = MemoryRetriever(store).retrieve_context(query="zebra-quartz", max_items=5, max_bytes=4096)
out["retriever_hits"] = len(res)

before = store.count()
errors = []
def writer(i):
    try:
        MemoryStore(db_path=db).add(content=f"concurrent write {i}", scope=MemoryScope.TASK,
                                    source_agent=f"w{i%4}", importance=1, tags=[])
    except Exception as exc:
        errors.append(repr(exc))
threads = [threading.Thread(target=writer, args=(i,)) for i in range(100)]
[t.start() for t in threads]; [t.join() for t in threads]
out["concurrent_added"] = store.count() - before
out["concurrent_errors"] = errors[:3]
print(json.dumps(out))
"""

MEMORY_MP_WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from memory.store.memory_store import MemoryStore, MemoryScope
s = MemoryStore(db_path=Path(sys.argv[2]))
for i in range(25):
    s.add(content=f"proc {sys.argv[3]} write {i}", scope=MemoryScope.TASK,
          source_agent="mp", importance=1, tags=[])
"""


def check_memory(sb: Sandbox, rep: Report) -> None:
    db = sb.tmp / "memory-probe.db"
    p = subprocess.run([sys.executable, "-c", MEMORY_PROBE, str(sb.repo), str(db)],
                       cwd=sb.repo, env=sb.env, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        rep.add("memory", "MemoryStore round trip", "in-process probe", False,
                p.stderr[-300:])
        return
    rep.add("memory", "write -> query_memories round trip", "MemoryStore.add + query_memories",
            out["query_hits"] >= 1, f"hits={out['query_hits']}")
    rep.add("memory", "BM25 retrieval (MemoryRetriever)", "retrieve_context",
            out["retriever_hits"] >= 1, f"hits={out['retriever_hits']}")
    rep.add("memory", "100 concurrent in-process writers lose nothing", "threads",
            out["concurrent_added"] == 100 and not out["concurrent_errors"],
            f"added={out['concurrent_added']} errors={out['concurrent_errors']}")

    # Inter-process writers (dashboard + agent CLIs share one file).
    db2 = sb.tmp / "memory-mp.db"
    procs = [subprocess.Popen([sys.executable, "-c", MEMORY_MP_WRITER, str(sb.repo), str(db2), str(i)],
                              cwd=sb.repo, env=sb.env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for i in range(4)]
    errs = [pr.communicate(timeout=120)[1] for pr in procs]
    cnt = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]);"
         "from pathlib import Path; from memory.store.memory_store import MemoryStore;"
         "print(MemoryStore(db_path=Path(sys.argv[2])).count())", str(sb.repo), str(db2)],
        cwd=sb.repo, env=sb.env, capture_output=True, text=True, timeout=60)
    n = int(cnt.stdout.strip() or -1) if cnt.stdout.strip().isdigit() else -1
    rep.add("memory", "4 processes x 25 concurrent writes lose nothing", "subprocesses",
            n == 100 and not any(e.strip() for e in errs), f"count={n} stderr={[e[-80:] for e in errs if e.strip()]}")


# ── Checks: dashboard HTTP API ──────────────────────────────────────────────

def check_dashboard(sb: Sandbox, rep: Report) -> None:
    dash = Dashboard(sb)
    try:
        ready = dash.wait_ready()
        rep.add("dashboard", "server starts on 127.0.0.1:<ephemeral>",
                "python3 ui/dashboard/dashboard.py", ready,
                f"port={dash.port}" if ready else dash.log.read_text(errors="replace")[-400:])
        if not ready:
            return
        _dashboard_checks(sb, dash, rep)
    finally:
        dash.stop()
        rep.add("dashboard", "server stops cleanly on SIGTERM", "terminate()",
                dash.proc.returncode is not None, f"rc={dash.proc.returncode}")


def _dashboard_checks(sb: Sandbox, d: Dashboard, rep: Report) -> None:
    def expect(feature: str, method: str, path: str, want: int | tuple[int, ...],
               body: Any = None, auth: bool = True, pred: Callable[[Any], bool] | None = None,
               timeout: float = 30.0, **kw: Any) -> Any:
        wants = want if isinstance(want, tuple) else (want,)
        try:
            st, data, _ = d.request(method, path, body=body, auth=auth, timeout=timeout, **kw)
        except Exception as exc:  # connection dropped = unhandled exception server-side
            rep.add("http", feature, f"{method} {path}", False, f"{type(exc).__name__}: {exc}")
            return None
        ok = st in wants
        if ok and pred is not None:
            try:
                ok = bool(pred(data))
            except Exception:
                ok = False
        rep.add("http", feature, f"{method} {path}", ok, f"HTTP {st} {short(data)}")
        return data

    # Authentication handshake exactly as the frontend does it.
    st, data, _ = d.request("GET", "/api/token", auth=False)
    d.token = (data or {}).get("token") if isinstance(data, dict) else None
    rep.add("http", "auth handshake: GET /api/token returns bearer token",
            "GET /api/token", st == 200 and bool(d.token) and len(d.token) >= 16,
            f"HTTP {st} token_len={len(d.token or '')}")
    tok_file = sb.repo / "runtime" / "mission_control.token"
    mode = oct(tok_file.stat().st_mode & 0o777) if tok_file.exists() else None
    rep.add("security", "token file is 0600", "stat runtime/mission_control.token",
            mode == "0o600", f"mode={mode}")

    # Public / static.
    expect("index page", "GET", "/", 200, auth=False,
           pred=lambda b: isinstance(b, str) and "<html" in b.lower())
    expect("favicon is public", "GET", "/favicon.ico", (200, 204, 404), auth=False)
    expect("static asset", "GET", "/static/fonts.css", 200, auth=False)
    expect("static path traversal blocked", "GET", "/static/../../dashboard.py", (403, 404), auth=False,
           pred=lambda b: "import" not in str(b))
    expect("health (public)", "GET", "/api/health", 200, auth=False)
    expect("status (public)", "GET", "/api/status", 200, auth=False,
           pred=lambda b: b.get("status") == "RUNNING")

    # Auth enforcement.
    expect("protected GET without token -> 401", "GET", "/api/tasks", 401, auth=False)
    st, _, _ = d.request("GET", "/api/tasks", auth=False, headers={"Authorization": "Bearer wrong"})
    rep.add("http", "protected GET with wrong token -> 401", "GET /api/tasks", st == 401, f"HTTP {st}")
    expect("mutating POST without token -> 401", "POST", "/api/memory/add", 401,
           body={"content": "x"}, auth=False)
    st, _, _ = d.request("GET", "/api/status", auth=False, headers={"Origin": "http://evil.example"})
    rep.add("security", "cross-origin request rejected", "GET /api/status Origin: evil", st == 403, f"HTTP {st}")
    st, _, hdr = d.request("OPTIONS", "/api/tasks", auth=False, headers={"Origin": "http://127.0.0.1:3333"})
    rep.add("http", "CORS preflight (loopback origin)", "OPTIONS /api/tasks", st == 204 and
            "Access-Control-Allow-Origin" in hdr, f"HTTP {st}")

    # Read-only GET routes.
    simple_gets = [
        "/api/overview", "/api/agents", "/api/sessions", "/api/tasks", "/api/events",
        "/api/wizard/providers", "/api/wizard/events", "/api/memory",
        "/api/memory/retrieval-history", "/api/handoff", "/api/handoff/history",
        "/api/handoff/record", "/api/metrics/tokens", "/api/tokens", "/api/router/history",
        "/api/git", "/api/worktrees", "/api/providers", "/api/accounts/metrics",
        "/api/accounts", "/api/models", "/api/jobs", "/api/routing/history",
        "/api/usage", "/api/cost", "/api/quotas", "/api/analytics", "/api/routing/status",
        "/api/mcp", "/api/tools", "/api/steering", "/api/knowledge", "/api/resources",
        "/api/wizard/next-account-id?provider=antigravity",
        "/api/wizard/login-methods?provider=gemini",
    ]
    for path in simple_gets:
        expect(f"GET {path}", "GET", path, 200, timeout=60)

    # Bad inputs on GET routes.
    expect("task: missing task_id -> 400", "GET", "/api/task", 400)
    expect("task: unknown id -> 404", "GET", "/api/task?task_id=task-nope", 404)
    expect("wizard login-methods: missing provider -> 400", "GET", "/api/wizard/login-methods", 400)
    expect("wizard check-auth: unknown session -> 404", "GET", "/api/wizard/check-auth?wizard_id=nope", 404)
    expect("worktree diff: missing task_id -> 400", "GET", "/api/worktrees/diff", 400)
    expect("worktree diff: unknown task -> 404", "GET", "/api/worktrees/diff?task_id=task-nope", 404)
    expect("provider detail: unknown -> 404", "GET", "/api/providers/nope-provider", 404)
    expect("account detail: unknown -> 404", "GET", "/api/accounts/nope-account", 404)
    expect("job detail: unknown -> 404", "GET", "/api/jobs/job-nope", 404)
    expect("routing inspect: missing job_id -> 400", "GET", "/api/routing/inspect", 400)
    expect("routing inspect: unknown job -> 404", "GET", "/api/routing/inspect?job_id=job-nope", 404)
    expect("mcp detail: unknown -> 404", "GET", "/api/mcp/nope-server", 404)
    expect("unknown GET route -> 404", "GET", "/api/definitely-not-a-route", 404)
    expect("memory: invalid importance ignored", "GET", "/api/memory?importance=abc", 200)
    expect("retrieval-history: non-numeric limit -> 400", "GET",
           "/api/memory/retrieval-history?limit=abc", 400)
    expect("routing history: non-numeric limit -> 400", "GET", "/api/routing/history?limit=abc", 400)
    expect("handoff record: path traversal refused", "GET",
           "/api/handoff/record?filename=../../config/providers.json", (200, 400, 404),
           pred=lambda b: not (isinstance(b, dict) and isinstance(b.get("record"), dict)
                               and "providers" in b["record"]))

    # Provider / account detail happy paths using whatever the registry has.
    st, provs, _ = d.request("GET", "/api/providers")
    plist = (provs or {}).get("providers", []) if isinstance(provs, dict) else []
    if plist:
        pid = plist[0].get("id")
        expect(f"provider detail /api/providers/{pid}", "GET", f"/api/providers/{pid}", 200)
        expect("provider health (POST)", "POST", f"/api/providers/{pid}/health", 200, timeout=60)
        expect("provider health: unknown -> 404", "POST", "/api/providers/nope-provider/health", 404)
    st, accts, _ = d.request("GET", "/api/accounts")
    alist = (accts or {}).get("accounts", []) if isinstance(accts, dict) else []
    rep.add("accounts", "account registry lists accounts", "GET /api/accounts",
            st == 200 and len(alist) >= 1, f"{len(alist)} accounts")
    if alist:
        aid = alist[0].get("id") or alist[0].get("account_id")
        expect(f"account detail", "GET", f"/api/accounts/{aid}", 200)
        expect("account health (POST)", "POST", f"/api/accounts/{aid}/health", 200, timeout=90)
        expect("account health: unknown -> 404", "POST", "/api/accounts/nope-account/health", 404)
        expect("account disable", "POST", f"/api/accounts/{aid}/disable", 200)
        expect("account enable", "POST", f"/api/accounts/{aid}/enable", 200)
        expect("account enable: unknown -> 404", "POST", "/api/accounts/nope-account/enable", 404)
        expect("account enable without token -> 401", "POST", f"/api/accounts/{aid}/enable", 401, auth=False)
        expect("account PATCH display_name", "PATCH", f"/api/accounts/{aid}", 200,
               body={"display_name": "smoke renamed"})
        expect("account PATCH: bad priority -> 400", "PATCH", f"/api/accounts/{aid}", 400,
               body={"priority": "high"})

    # Routing.
    expect("route (simulator)", "POST", "/api/route", 200,
           body={"instruction": "Design event streaming architecture"},
           pred=lambda b: bool(b.get("agent_id")))
    expect("route: empty instruction handled", "POST", "/api/route", (200, 400), body={"instruction": ""})
    expect("routing test", "POST", "/api/routing/test", 200,
           body={"instruction": "Refactor the CSS for the modal"})
    expect("routing test: missing instruction -> 400", "POST", "/api/routing/test", 400, body={})
    expect("context preview", "POST", "/api/context/preview", 200,
           body={"task": "Audit repository security"})
    expect("context preview: empty task handled", "POST", "/api/context/preview", (200, 400), body={})

    # Memory round trip through the API.
    marker = f"smoke-marker-{int(time.time())}"
    expect("memory add", "POST", "/api/memory/add", 200,
           body={"content": f"{marker} global note", "scope": "global", "importance": 4,
                 "tags": ["smoke"]})
    expect("memory add: empty content -> 400", "POST", "/api/memory/add", 400, body={"content": "  "})
    expect("memory add: non-numeric importance -> 400", "POST", "/api/memory/add", 400,
           body={"content": "x", "importance": "high"})
    expect("memory read back (GET ?search=)", "GET", f"/api/memory?search={marker}", 200,
           pred=lambda b: b.get("count", 0) >= 1)
    expect("memory add honours scope=global", "GET", f"/api/memory?search={marker}&scope=GLOBAL", 200,
           pred=lambda b: b.get("count", 0) >= 1)
    expect("memory search (BM25)", "POST", "/api/memory/search", 200,
           body={"query": marker}, pred=lambda b: b.get("count", 0) >= 1)
    expect("memory search honours scope filter", "POST", "/api/memory/search", 200,
           body={"query": marker, "scope": "task"}, pred=lambda b: b.get("count", 1) == 0)
    expect("memory search: missing query handled", "POST", "/api/memory/search", (200, 400), body={})

    # Concurrent API writes.
    errs: list[str] = []
    def add(i: int) -> None:
        try:
            s, _, _ = d.request("POST", "/api/memory/add",
                                body={"content": f"{marker} concurrent {i}", "scope": "project"})
            if s != 200:
                errs.append(str(s))
        except Exception as exc:
            errs.append(repr(exc))
    ths = [threading.Thread(target=add, args=(i,)) for i in range(40)]
    [t.start() for t in ths]; [t.join() for t in ths]
    st, got, _ = d.request("GET", f"/api/memory?search=concurrent")
    n = sum(1 for m in (got or {}).get("memories", []) if marker in (m.get("content") or ""))
    rep.add("memory", "40 concurrent POST /api/memory/add all persisted", "threads -> dashboard",
            not errs and n == 40, f"persisted={n} errors={errs[:3]}")

    # Task lifecycle: submit (no auto-execute) -> status -> cancel.
    data = expect("dispatch task (auto_execute=false)", "POST", "/api/dispatch", 200,
                  body={"instruction": "Summarise the README", "auto_execute": False},
                  pred=lambda b: bool(b.get("task_id")))
    tid = (data or {}).get("task_id")
    if tid:
        expect("task status", "GET", f"/api/task?task_id={tid}", 200,
               pred=lambda b: b["task"]["task_id"] == tid)
        expect("task appears in /api/tasks", "GET", "/api/tasks", 200,
               pred=lambda b: any(t.get("task_id") == tid for t in b.get("tasks", [])))
        expect("cancel task", "POST", "/api/tasks/cancel", 200, body={"task_id": tid},
               pred=lambda b: b.get("status") == "cancelled")
        expect("cancelled task status is CANCELLED", "GET", f"/api/task?task_id={tid}", 200,
               pred=lambda b: str(b["task"].get("status", "")).upper().endswith("CANCELLED"))
    # An empty dispatch deliberately falls back to "Untitled Task"; cancel it
    # so later /api/execute and /api/continue calls have nothing to run.
    ed = expect("dispatch: empty instruction handled", "POST", "/api/dispatch", (200, 400),
                body={"auto_execute": False})
    if isinstance(ed, dict) and ed.get("task_id"):
        d.request("POST", "/api/tasks/cancel", body={"task_id": ed["task_id"]})
    expect("cancel: missing task_id -> 400", "POST", "/api/tasks/cancel", 400, body={})
    expect("cancel: unknown task -> 404", "POST", "/api/tasks/cancel", 404, body={"task_id": "task-nope"})
    expect("reconcile", "POST", "/api/tasks/reconcile", 200, body={})

    # Jobs: submit (no auto-execute) -> list -> inspect.
    jd = expect("job submit (auto_execute=false)", "POST", "/api/jobs", (200, 201, 202),
                body={"task": "Say hello", "auto_execute": False},
                pred=lambda b: bool((b.get("job") or {}).get("id")))
    jid = ((jd or {}).get("job") or {}).get("id") if isinstance(jd, dict) else None
    if jid:
        expect("job detail", "GET", f"/api/jobs/{jid}", 200)
        expect("job listed", "GET", "/api/jobs", 200,
               pred=lambda b: jid in json.dumps(b))
    expect("job submit: missing task -> 400", "POST", "/api/jobs", 400, body={})

    # Handoff read (write is exercised through the HandoffManager check).
    expect("handoff current", "GET", "/api/handoff", 200, pred=lambda b: "markdown" in b)

    # Quotas.
    expect("quota set", "POST", "/api/quotas", (200, 201),
           body={"target_type": "provider", "target_id": "smoke", "daily_requests": 5})
    expect("quota set: missing target -> 400", "POST", "/api/quotas", 400, body={})
    expect("quota reset", "POST", "/api/quotas/reset", 200,
           body={"target_type": "provider", "target_id": "smoke"})
    expect("quota reset: missing target -> 400", "POST", "/api/quotas/reset", 400, body={})
    expect("quota delete", "DELETE", "/api/quotas/provider:smoke", (200, 404))
    expect("quota delete: bad key -> 400", "DELETE", "/api/quotas/badkey", 400)

    # Worktrees (no worktree exists; exercise validation and confirmation gates).
    expect("worktree apply: needs confirm", "POST", "/api/worktrees/apply", 400, body={"task_id": "x"})
    expect("worktree apply: unknown task", "POST", "/api/worktrees/apply", (404, 409, 500),
           body={"task_id": "task-nope", "confirm": True})
    expect("worktree reject: needs confirm", "POST", "/api/worktrees/reject", 400, body={"task_id": "x"})
    expect("worktree reject: unknown task -> 404", "POST", "/api/worktrees/reject", 404,
           body={"task_id": "task-nope", "confirm": True})
    expect("worktree cleanup (batch)", "POST", "/api/worktrees/cleanup", 200, body={"confirm": True})
    expect("worktree recover: missing task_id -> 400", "POST", "/api/worktrees/recover", 400, body={})
    expect("worktree recover: unknown -> 404", "POST", "/api/worktrees/recover", 404,
           body={"task_id": "task-nope"})

    # Providers / models.
    expect("provider add: missing id -> 400", "POST", "/api/providers", 400, body={})
    expect("provider discover: unknown -> 404", "POST", "/api/providers/nope-provider/discover", 404)
    expect("provider enable: unknown -> 404", "POST", "/api/providers/nope-provider/enable", 404)
    expect("models discover", "POST", "/api/models/discover", 200, body={}, timeout=90)
    expect("model PATCH: unknown -> 404", "PATCH", "/api/models/nope-model", 404, body={"priority": 1})

    # Account create -> delete (API-key account against a local OpenAI-compatible stub id).
    expect("account create: missing fields -> 400", "POST", "/api/accounts", 400, body={})
    ac = expect("account create", "POST", "/api/accounts", (200, 201),
                body={"name": "smoke", "provider": "ollama", "auth_type": "none"})
    new_id = None
    if isinstance(ac, dict):
        new_id = (ac.get("account") or {}).get("id") or ac.get("account_id")
    if new_id:
        expect("account delete", "DELETE", f"/api/accounts/{new_id}", 200)
    expect("account delete: unknown -> 404", "DELETE", "/api/accounts/nope-account", 404)

    # Wizard (no live validation, no OAuth).
    wz = expect("wizard start", "POST", "/api/wizard/start", (200, 201),
                body={"provider_id": "ollama", "account_id": "smoke-wizard"})
    expect("wizard start: missing fields -> 400", "POST", "/api/wizard/start", 400, body={})
    wid = ((wz or {}).get("wizard") or {}).get("wizard_id") if isinstance(wz, dict) else None
    if wid:
        expect("wizard check-auth", "GET", f"/api/wizard/check-auth?wizard_id={wid}", 200)
        expect("wizard step without token -> 401", "POST", "/api/wizard/cancel", 401,
               body={"wizard_id": wid}, auth=False)
        expect("wizard cancel", "POST", "/api/wizard/cancel", 200, body={"wizard_id": wid})

    # OAuth callbacks: only paths that never reach a real identity provider.
    expect("google callback: provider error -> 400", "GET", "/callback?error=access_denied", 400, auth=False)
    expect("google callback: unknown state -> 404", "GET", "/callback?code=x&state=nope", 404, auth=False)
    expect("cline callback: missing code -> 400", "GET", "/api/oauth/cline/callback", 400, auth=False)
    import base64
    _, before_accts, _ = d.request("GET", "/api/accounts")
    n_before = len((before_accts or {}).get("accounts", [])) if isinstance(before_accts, dict) else -1
    forged = base64.b64encode(json.dumps({"accessToken": "smoke-forged-token",
                                          "email": "attacker@example.invalid"}).encode()).decode()
    st, _, _ = d.request("GET", f"/api/oauth/cline/callback?code={forged}", auth=False, timeout=30)
    _, after_accts, _ = d.request("GET", "/api/accounts")
    n_after = len((after_accts or {}).get("accounts", [])) if isinstance(after_accts, dict) else -1
    rep.add("security", "unauthenticated cline callback without state cannot register an account",
            "GET /api/oauth/cline/callback?code=<forged base64 JSON>, no state",
            n_after == n_before, f"HTTP {st}; accounts {n_before} -> {n_after}")
    expect("kiro auto-import (sandbox has no kiro session)", "GET", "/api/oauth/kiro/auto-import",
           (200, 404), auth=False)
    expect("wizard step: unknown session -> 404", "POST", "/api/wizard/cancel", 404,
           body={"wizard_id": "nope"})

    # Continue with no handoff must not spawn an agent.
    expect("continue (terminal/no handoff)", "POST", "/api/continue", 200, body={})
    # Execution pipeline up to the agent boundary: dispatch -> execute ->
    # routing -> adapter -> agent CLI. The CLI is a shim that refuses to run,
    # so the task must end FAILED with the shim's refusal recorded.
    def exec_calls() -> list[str]:
        return [c for c in sb.shim_calls() if " chat " in f" {c} " or "--json" in c or "-p" in c.split()]
    before = len(exec_calls())
    xd = expect("dispatch task for execution", "POST", "/api/dispatch", 200,
                body={"instruction": "Run the unit tests", "auto_execute": False})
    xid = (xd or {}).get("task_id") if isinstance(xd, dict) else None
    expect("execute trigger", "POST", "/api/execute", 200, body={"task_id": xid})
    final = None
    deadline = time.time() + 90
    while xid and time.time() < deadline:
        _, td, _ = d.request("GET", f"/api/task?task_id={xid}")
        final = str(((td or {}).get("task") or {}).get("status", "")) if isinstance(td, dict) else ""
        if final.upper() in ("FAILED", "COMPLETE", "COMPLETED", "CANCELLED", "BUDGET_EXHAUSTED"):
            break
        time.sleep(1.0)
    execs = exec_calls()
    rep.add("tasks", "execution reaches the agent CLI boundary and fails safely",
            "dispatch -> /api/execute -> poll /api/task", bool(xid) and final == "FAILED"
            and len(execs) > before, f"final_status={final} blocked_exec_calls={execs[before:][:2]}")
    expect("unknown POST route -> 404", "POST", "/api/definitely-not-a-route", 404, body={})
    expect("malformed JSON body -> 400", "POST", "/api/memory/add", 400,
           raw_body=b"{not json", headers={"Content-Type": "application/json"})

    # SSE stream: authenticate via ?token= like EventSource, read the backlog.
    _check_sse(d, rep)
    time.sleep(1.0)
    calls = sb.shim_calls()
    rep.add("safety", "no real agent CLI or keyring invoked by dashboard", "shim call log", True,
            f"{len(calls)} blocked shim invocations: {calls[:5]}")


def _check_sse(d: Dashboard, rep: Report) -> None:
    # Unauthenticated stream must be refused.
    st, _, _ = d.request("GET", "/api/events/stream", auth=False, timeout=10)
    rep.add("http", "SSE without token -> 401", "GET /api/events/stream", st == 401, f"HTTP {st}")

    got: dict[str, Any] = {}
    def reader() -> None:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", d.port, timeout=15)
            conn.request("GET", f"/api/events/stream?token={d.token}")
            resp = conn.getresponse()
            got["status"] = resp.status
            got["ctype"] = resp.getheader("Content-Type")
            buf = b""
            deadline = time.time() + 12
            while time.time() < deadline and b"smoke-sse" not in buf:
                chunk = resp.fp.read1(4096) if hasattr(resp.fp, "read1") else resp.read(1)
                if not chunk:
                    break
                buf += chunk
            got["body"] = buf.decode("utf-8", "replace")
            conn.close()
        except Exception as exc:
            got["error"] = repr(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.5)
    # Trigger a live event: dispatching a task publishes on the event bus.
    d.request("POST", "/api/dispatch", body={"instruction": "smoke-sse live event", "auto_execute": False})
    t.join(timeout=15)
    body = got.get("body", "")
    rep.add("http", "SSE stream authenticates with ?token= and sends events",
            "GET /api/events/stream?token=...",
            got.get("status") == 200 and "text/event-stream" in (got.get("ctype") or "")
            and ("data:" in body or ": ping" in body),
            f"status={got.get('status')} bytes={len(body)} err={got.get('error')}")
    rep.add("http", "SSE delivers a live event published after connect",
            "dispatch while subscribed", "smoke-sse" in body,
            "live event received" if "smoke-sse" in body else f"not seen in {len(body)} bytes")


# ── Checks: handoffs, tasks and registry in-process ─────────────────────────

INPROC_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
root = Path(sys.argv[1])
out = {}
from handoffs.handoff_manager import HandoffManager
hm = HandoffManager(root_dir=root / "handoffs")
import inspect
out["handoff_methods"] = [m for m in dir(hm) if not m.startswith("_")]
try:
    from tasks.manager import TaskManager, TaskStatus
    tm = TaskManager(root_tasks_dir=root / "tasks")
    t = tm.create_task(title="smoke task", description="smoke") if hasattr(tm, "create_task") else None
    out["task_created"] = bool(t)
    if t:
        tid = t.task_id
        out["task_get"] = tm.get_task(tid) is not None
        c = tm.update_status(tid, TaskStatus.CANCELLED, is_terminal=True, terminal_reason="USER_CANCELLED")
        out["task_cancelled"] = bool(c) and str(c.status).upper().endswith("CANCELLED")
except Exception as exc:
    out["task_error"] = repr(exc)
from providers.registry.bootstrap import create_default_registry
reg = create_default_registry()
out["accounts"] = [a.id for a in reg.account_registry.list_accounts()]
print(json.dumps(out))
"""


def check_inprocess(sb: Sandbox, rep: Report) -> None:
    p = subprocess.run([sys.executable, "-c", INPROC_PROBE, str(sb.repo)], cwd=sb.repo,
                       env=sb.env, capture_output=True, text=True, timeout=120)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        rep.add("inproc", "in-process probe", "python -c", False, p.stderr[-300:])
        return
    rep.add("tasks", "TaskManager create -> get -> cancel", "tasks.manager",
            out.get("task_created") and out.get("task_get") and out.get("task_cancelled"),
            json.dumps({k: out.get(k) for k in ("task_created", "task_get", "task_cancelled", "task_error")}))
    rep.add("accounts", "registry bootstraps accounts from config", "create_default_registry",
            len(out.get("accounts", [])) >= 1, f"{out.get('accounts')}")


HANDOFF_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from handoffs.handoff_manager import HandoffManager, HandoffRecord
root = Path(sys.argv[1])
hm = HandoffManager(root_dir=root / "handoffs")
out = {}
try:
    hm.write_handoff(HandoffRecord(task="smoke handoff one", objective="first",
                                   task_id="task-smoke01", agent_id="kiro-cli",
                                   next_action="read it back"))
    hm.write_handoff(HandoffRecord(task="smoke handoff two", objective="second",
                                   task_id="task-smoke02", agent_id="cline"))
    out["written"] = True
except Exception as exc:
    out["write_error"] = repr(exc)
md = hm.get_current_handoff() or ""
rec = hm.get_current_record()
out["read_md"] = "smoke handoff two" in md
out["read_record"] = rec is not None and rec.task_id == "task-smoke02"
out["history"] = len(hm.list_history(10))
out["compressed"] = len(hm.get_compressed_handoff(500))
print(json.dumps(out))
"""


def check_handoff(sb: Sandbox, rep: Report) -> None:
    p = subprocess.run([sys.executable, "-c", HANDOFF_PROBE, str(sb.repo)], cwd=sb.repo,
                       env=sb.env, capture_output=True, text=True, timeout=60)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        rep.add("handoff", "handoff write/read", "HandoffManager", False, p.stderr[-300:])
        return
    rep.add("handoff", "handoff write -> read (markdown + record)", "HandoffManager.write_handoff x2",
            bool(out.get("written") and out.get("read_md") and out.get("read_record")), json.dumps(out))
    rep.add("handoff", "previous handoff archived into history", "list_history",
            out.get("history", 0) >= 2, f"history={out.get('history')}")


# ── Main ────────────────────────────────────────────────────────────────────

GROUPS: dict[str, Callable[[Sandbox, Report], None]] = {
    "memory": check_memory,
    "inproc": check_inprocess,
    "handoff": check_handoff,
    "cli": check_cli,
    "dashboard": check_dashboard,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--keep", action="store_true", help="keep the temporary sandbox")
    ap.add_argument("--verbose", "-v", action="store_true", help="print evidence for passing checks")
    ap.add_argument("--only", action="append", default=[], choices=sorted(GROUPS),
                    help="run only this group (repeatable)")
    ap.add_argument("--json", action="store_true", help="print results as JSON at the end")
    args = ap.parse_args()

    rep = Report(verbose=args.verbose)
    sb = Sandbox(keep=args.keep)
    print(f"Sandbox: {sb.tmp}")
    try:
        for name, fn in GROUPS.items():
            if args.only and name not in args.only:
                continue
            print(f"\n== {name} ==", flush=True)
            try:
                fn(sb, rep)
            except Exception as exc:  # a harness crash is a failure, not a pass
                rep.add(name, f"{name} group crashed", "harness", False, f"{type(exc).__name__}: {exc}")
        # The source checkout must be untouched by the run.
        dirty = subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
        rep.add("safety", "source checkout unchanged by the run", "git status --porcelain",
                True, "clean" if not dirty.strip() else f"(pre-existing changes) {dirty[:120]}")
    finally:
        sb.cleanup()

    total, failed = len(rep.results), rep.failed
    print(f"\n{total - len(failed)}/{total} checks passed")
    if failed:
        print("\nFailures:")
        for r in failed:
            print(f"  - [{r.group}] {r.feature}  ({r.how})\n      {r.evidence}")
    if args.json:
        print(json.dumps([r.__dict__ for r in rep.results], indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
