#!/usr/bin/env python3
"""Environment analysis for the shared brain — "can this host do the work?".

Why this exists
---------------
`bm doctor` answers a narrow question: is basic-memory's file<->index state
consistent. It says nothing about whether *this machine* can actually run the
swarm. That is the question a human asks when a handoff lands on a new host and
nothing happens: which agents are installed, is the brain MCP server wired into
them, is any provider account configured, and — the bottom line — can a task
run here at all.

This module answers that question end to end and prints a sectioned report. It
deliberately makes no changes: it only inspects. It exits non-zero *only* on a
genuine blocker (no worker path of any kind), never merely because an optional
tool is absent.

Design rules
------------
* Stdlib only. This has to run on whatever Python the host ships, including the
  degraded case where PATH is stripped to /usr/bin:/bin. Adding a dependency to
  a diagnostic that must run everywhere would defeat its purpose.
* Every external command is guarded by a short timeout. A hung `--version` on
  one tool must never hang the whole report.
* API keys are referenced by *source* only (env var name, config file). A key
  value is never printed, logged, or placed in the JSON output.
* Detection over assumption. The set of installed IDEs/CLIs is probed on this
  host, never hardcoded — the report is meant to be truthful on any machine.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# Import the provider catalog as a sibling module. doctor.py and providers.py
# always live in the same directory, so make that directory importable rather
# than assuming the caller's cwd. This keeps `brain doctor` working regardless
# of where it is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import providers  # sibling module, stdlib-only, no key values leak out
except Exception as exc:  # pragma: no cover - providers is expected to import
    providers = None
    _PROVIDERS_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _PROVIDERS_IMPORT_ERROR = ""

HOME = Path.home()
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", HOME / "agentic-brain")).resolve()

# A subprocess in a diagnostic is a liability: it can hang, it can be missing,
# it can spew. Cap it hard and swallow every failure into a printable string.
SUBPROC_TIMEOUT = 5.0


def _run(cmd: list[str], timeout: float = SUBPROC_TIMEOUT) -> tuple[bool, str]:
    """Run a command defensively. Return (ok, first_line_of_output).

    Never raises. A missing binary, a timeout, or a non-zero exit all collapse
    into (False, reason) so the report can render them uniformly.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return False, "not found"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g}s"
    except Exception as exc:  # defensive: never let a probe crash the report
        return False, f"{type(exc).__name__}: {exc}"
    # Many CLIs print their version to stderr; take whichever stream has content.
    out = (proc.stdout or proc.stderr or "").strip()
    first = out.splitlines()[0] if out else ""
    return proc.returncode == 0, first


def _which_with_fallbacks(binary: str, extra_paths: list[str]) -> str | None:
    """Resolve a binary via PATH, then a list of well-known absolute locations.

    shutil.which respects PATH, which is enough on a normal login shell. But an
    agent (or the portability probe) may launch us with a trimmed PATH, so we
    additionally check install locations the tool is known to land in.
    """
    found = shutil.which(binary)
    if found:
        return found
    for candidate in extra_paths:
        p = Path(os.path.expanduser(candidate))
        if p.exists() and os.access(p, os.X_OK):
            return str(p)
    return None


# ---------------------------------------------------------------------------
# 1. HOST
# ---------------------------------------------------------------------------
def _basic_memory_interpreter() -> tuple[str | None, str]:
    """Find the interpreter that can import basic_memory.

    Reused approach from sentinel._basic_memory_interpreter: basic-memory runs
    in its own environment (uv tool / pipx / venv), which is usually NOT the
    interpreter running this script. Resolve it from the `bm` launcher's
    shebang so the answer is correct on any install layout. Returns
    (interpreter_path, how_it_was_found).
    """
    override = os.environ.get("BM_PYTHON")
    if override and Path(override).exists():
        return override, "BM_PYTHON override"
    bm = (
        shutil.which("bm")
        or os.environ.get("BM_BIN")
        or str(HOME / ".local" / "bin" / "bm")
    )
    try:
        first_line = Path(bm).read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first_line.startswith("#!"):
            candidate = first_line[2:].strip().split()[0]
            if candidate and Path(candidate).exists():
                return candidate, f"shebang of {bm}"
    except Exception:
        pass
    return None, "not resolvable (bm launcher not found)"


def collect_host() -> dict:
    uname = platform.uname()
    bm_bin = _which_with_fallbacks(
        "basic-memory", [str(HOME / ".local/bin/basic-memory")]
    )
    bm_launcher = _which_with_fallbacks("bm", [str(HOME / ".local/bin/bm")])
    interp, interp_how = _basic_memory_interpreter()

    # Prove the resolved interpreter can actually import basic_memory, rather
    # than just existing. This is the check that catches a broken install.
    can_import = False
    import_detail = "interpreter not resolved"
    if interp:
        ok, out = _run([interp, "-c",
                        "import basic_memory; "
                        "print(getattr(basic_memory, '__version__', 'ok'))"])
        can_import = ok
        import_detail = out if out else ("import ok" if ok else "import failed")

    return {
        "os": f"{uname.system} {uname.release}",
        "kernel": uname.version.split()[0] if uname.version else uname.release,
        "machine": uname.machine,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "bm_found": bm_bin is not None,
        "bm_path": bm_bin,
        "bm_launcher": bm_launcher,
        "basic_memory_interpreter": interp,
        "basic_memory_interpreter_source": interp_how,
        "basic_memory_importable": can_import,
        "basic_memory_import_detail": import_detail,
    }


# ---------------------------------------------------------------------------
# 2. IDEs DETECTED
# ---------------------------------------------------------------------------
# name -> (launcher binary, well-known absolute install paths to also try).
# The launcher is what shutil.which resolves; the fallback paths cover trimmed
# PATH and OS-specific install roots (Linux, macOS .app bundles).
IDE_TARGETS: dict[str, tuple[str, list[str]]] = {
    "VS Code": ("code", [
        "/usr/bin/code", "/usr/local/bin/code", "/snap/bin/code",
        "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    ]),
    "Antigravity IDE": ("antigravity-ide", [
        "/usr/bin/antigravity-ide", "/usr/local/bin/antigravity-ide",
        str(HOME / ".local/bin/antigravity-ide"),
    ]),
    "Kiro IDE": ("kiro", [
        "/usr/bin/kiro", "/usr/local/bin/kiro", str(HOME / ".local/bin/kiro"),
    ]),
    "Cursor": ("cursor", [
        "/usr/bin/cursor", "/usr/local/bin/cursor",
        "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
    ]),
    "Windsurf": ("windsurf", [
        "/usr/bin/windsurf", "/usr/local/bin/windsurf",
        "/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf",
    ]),
    "Zed": ("zed", [
        "/usr/bin/zed", "/usr/local/bin/zed", str(HOME / ".local/bin/zed"),
    ]),
    "JetBrains IDEA": ("idea", [
        "/usr/local/bin/idea", str(HOME / ".local/bin/idea"),
    ]),
    "JetBrains PyCharm": ("pycharm", [
        "/usr/local/bin/pycharm", str(HOME / ".local/bin/pycharm"),
    ]),
}


def collect_ides() -> list[dict]:
    rows = []
    for name, (binary, fallbacks) in IDE_TARGETS.items():
        path = _which_with_fallbacks(binary, fallbacks)
        rows.append({
            "name": name,
            "binary": binary,
            "found": path is not None,
            "path": path,
        })
    return rows


# ---------------------------------------------------------------------------
# 3. AGENT CLIs DETECTED
# ---------------------------------------------------------------------------
# name -> (binary, fallback paths, version args). Some CLIs have no cheap
# version flag or hang on one; version is best-effort and never blocks because
# _run caps every call with a timeout.
CLI_TARGETS: dict[str, tuple[str, list[str], list[str]]] = {
    "Cline": ("cline", [str(HOME / ".local/bin/cline"), "/usr/local/bin/cline"], ["--version"]),
    "Kiro CLI": ("kiro-cli", [str(HOME / ".local/bin/kiro-cli"), "/usr/local/bin/kiro-cli"], ["--version"]),
    "Antigravity CLI": ("agy", [str(HOME / ".local/bin/agy"), "/usr/local/bin/agy"], ["--version"]),
    "Amazon Q": ("q", [str(HOME / ".local/bin/q"), "/usr/local/bin/q"], ["--version"]),
    "Claude Code": ("claude", [str(HOME / ".local/bin/claude"), "/usr/local/bin/claude"], ["--version"]),
    "Codex": ("codex", [str(HOME / ".local/bin/codex"), "/usr/local/bin/codex"], ["--version"]),
    "Gemini CLI": ("gemini", [str(HOME / ".local/bin/gemini"), "/usr/local/bin/gemini"], ["--version"]),
}


def collect_agent_clis() -> list[dict]:
    rows = []
    for name, (binary, fallbacks, version_args) in CLI_TARGETS.items():
        path = _which_with_fallbacks(binary, fallbacks)
        version = None
        if path:
            ok, out = _run([path, *version_args])
            # Even a non-zero exit often prints something useful; keep it if so.
            version = out if out else ("(no version output)" if ok else None)
        rows.append({
            "name": name,
            "binary": binary,
            "found": path is not None,
            "path": path,
            "version": version,
        })
    return rows


# ---------------------------------------------------------------------------
# 4. MCP WIRING
# ---------------------------------------------------------------------------
# Reuse the exact config locations and container shape that
# register-brain-mcp.py writes, so this reads the same files that command
# writes. Mirrored by intent (label, path, container-key), not by import,
# because that module executes work in main() and we only want its layout.
def _repo_root() -> Path:
    # scripts/brain/doctor.py -> repo root is two parents up, mirroring
    # register-brain-mcp.py's REPO = parents[2].
    return Path(__file__).resolve().parents[2]


def _mcp_targets() -> list[tuple[str, Path, tuple[str, ...]]]:
    """(agent label, config path, container key path) — same as the registrar."""
    repo = _repo_root()
    return [
        ("Kiro CLI / Kiro VS Code (global)", HOME / ".kiro/settings/mcp.json", ("mcpServers",)),
        ("Kiro (workspace)", repo / ".kiro/settings/mcp.json", ("mcpServers",)),
        ("Amazon Q CLI / VS Code", HOME / ".aws/amazonq/mcp.json", ("mcpServers",)),
        ("Antigravity IDE / CLI", HOME / ".gemini/config/mcp_config.json", ("mcpServers",)),
        ("VS Code native (Copilot)", HOME / ".config/Code/User/mcp.json", ("servers",)),
        ("Cline (VS Code ext)", HOME / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json", ("mcpServers",)),
        ("Cline (standalone/CLI)", HOME / ".cline/data/settings/cline_mcp_settings.json", ("mcpServers",)),
    ]


BRAIN_PROJECT = "brain"  # the server key register-brain-mcp.py writes


def _dig_readonly(root: dict, keys: tuple[str, ...]) -> dict | None:
    node = root
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node if isinstance(node, dict) else None


def collect_mcp_wiring() -> list[dict]:
    rows = []
    for label, path, keys in _mcp_targets():
        exists = path.exists()
        registered = False
        detail = "config file absent"
        if exists:
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                container = _dig_readonly(data, keys) or {}
                registered = BRAIN_PROJECT in container
                detail = "brain registered" if registered else "config present, brain not registered"
            except json.JSONDecodeError:
                detail = "config present but invalid JSON"
            except Exception as exc:
                detail = f"unreadable: {type(exc).__name__}"
        rows.append({
            "agent": label,
            "config": str(path).replace(str(HOME), "~"),
            "config_exists": exists,
            "brain_registered": registered,
            "detail": detail,
        })
    return rows


# ---------------------------------------------------------------------------
# 5. PROVIDER ACCOUNTS
# ---------------------------------------------------------------------------
def collect_providers(test_accounts: bool) -> dict:
    """Summarise provider accounts. Never surfaces a key value — only sources.

    Delegates entirely to providers.py so the catalog and key-resolution logic
    live in one place. When --test-accounts is set, each *configured* account
    gets a free model-listing health check (no tokens consumed).
    """
    if providers is None:
        return {
            "available": False,
            "error": _PROVIDERS_IMPORT_ERROR or "providers module not importable",
            "accounts": [],
            "configured_count": 0,
        }
    accounts = providers.list_accounts()
    # Project only non-sensitive fields. list_accounts already omits key values,
    # but re-projecting here makes it structurally impossible for a key to leak.
    safe = []
    for a in accounts:
        row = {
            "provider": a.get("provider"),
            "label": a.get("label"),
            "configured": a.get("configured"),
            "billing": "FREE" if a.get("is_free_default") else "PAID",
            "free_type": a.get("free_type"),
            "key_source": a.get("key_source"),  # a SOURCE label, never the value
            "model": a.get("model"),
        }
        if test_accounts and a.get("configured"):
            res = providers.test_account(a["provider"])
            row["test"] = {
                "ok": res.get("ok"),
                "http": res.get("http"),
                "latency_ms": res.get("latency_ms"),
                "model_count": res.get("model_count"),
                "error": res.get("error") or "",
            }
        safe.append(row)
    configured = [a for a in safe if a["configured"]]
    return {
        "available": True,
        "accounts": safe,
        "configured_count": len(configured),
        "free_configured_count": sum(1 for a in configured if a["billing"] == "FREE"),
        "catalog_size": len(safe),
    }


# ---------------------------------------------------------------------------
# 6. BRAIN STORE
# ---------------------------------------------------------------------------
def collect_store() -> dict:
    """Inspect the store on disk directly — no basic-memory needed.

    The point of doctor is to work even when bm is broken, so this counts files
    rather than querying the index. handoff/current and a strict handoff schema
    are the two structural things the protocol depends on.
    """
    note_count = 0
    if BRAIN_DIR.exists():
        # Count Markdown notes across the store; cheap and portable.
        note_count = sum(1 for _ in BRAIN_DIR.rglob("*.md"))

    handoff = BRAIN_DIR / "handoff" / "current.md"
    schema = BRAIN_DIR / "schemas" / "handoff.md"
    schema_exists = schema.exists()
    schema_strict = False
    if schema_exists:
        try:
            text = schema.read_text(encoding="utf-8", errors="replace")
            # The protocol enforces `settings.validation: strict` in the schema
            # note; detect it without a YAML parser (stdlib only).
            schema_strict = ("validation: strict" in text
                             or "validation:strict" in text.replace(" ", ""))
        except Exception:
            pass

    return {
        "brain_dir": str(BRAIN_DIR),
        "brain_dir_exists": BRAIN_DIR.exists(),
        "note_count": note_count,
        "handoff_current_exists": handoff.exists(),
        "handoff_schema_exists": schema_exists,
        "handoff_schema_strict": schema_strict,
    }


# ---------------------------------------------------------------------------
# 7. VERDICT
# ---------------------------------------------------------------------------
def compute_verdict(clis: list[dict], provider_summary: dict) -> dict:
    """Answer the one question: can this host run swarm tasks?

    A worker exists if EITHER at least one agent CLI is installed OR at least
    one provider account is configured. A provider account alone is now a valid
    worker (the direct-API path in providers.chat), so a machine with only a key
    and no CLI can still run tasks.
    """
    installed_clis = [c["name"] for c in clis if c["found"]]
    configured = provider_summary.get("configured_count", 0)

    cli_path = bool(installed_clis)
    provider_path = configured > 0
    can_run = cli_path or provider_path

    paths = []
    if cli_path:
        paths.append(f"agent CLI ({', '.join(installed_clis)})")
    if provider_path:
        paths.append(f"{configured} provider account(s)")

    fix = None
    if not can_run:
        # Give the exact next command, not vague advice.
        fix = ("scripts/brain/brain providers add <provider>   "
               "# e.g. groq, gemini, cerebras — a free key becomes a worker")

    return {
        "can_run_swarm": can_run,
        "cli_worker_available": cli_path,
        "provider_worker_available": provider_path,
        "available_paths": paths,
        "installed_clis": installed_clis,
        "fix_command": fix,
    }


# ---------------------------------------------------------------------------
# Report assembly + rendering
# ---------------------------------------------------------------------------
def build_report(test_accounts: bool) -> dict:
    host = collect_host()
    ides = collect_ides()
    clis = collect_agent_clis()
    mcp = collect_mcp_wiring()
    provider_summary = collect_providers(test_accounts)
    store = collect_store()
    verdict = compute_verdict(clis, provider_summary)
    return {
        "host": host,
        "ides": ides,
        "agent_clis": clis,
        "mcp_wiring": mcp,
        "providers": provider_summary,
        "store": store,
        "verdict": verdict,
    }


def _mark(ok: bool) -> str:
    return "\u2713" if ok else "\u2717"  # tick / cross


def render_human(report: dict) -> str:
    lines: list[str] = []
    add = lines.append

    add("=" * 64)
    add("  brain doctor — host environment analysis")
    add("=" * 64)

    # 1. HOST
    h = report["host"]
    add("\n[1] HOST")
    add(f"    OS               {h['os']} ({h['machine']})")
    add(f"    kernel           {h['kernel']}")
    add(f"    python           {h['python_version']}  ({h['python_executable']})")
    add(f"    basic-memory     {_mark(h['bm_found'])} {h['bm_path'] or 'not found'}")
    add(f"    bm interpreter   {_mark(h['basic_memory_importable'])} "
        f"{h['basic_memory_interpreter'] or 'unresolved'}")
    add(f"                     source: {h['basic_memory_interpreter_source']}; "
        f"import: {h['basic_memory_import_detail']}")

    # 2. IDEs
    add("\n[2] IDEs DETECTED")
    for r in report["ides"]:
        add(f"    {_mark(r['found'])} {r['name']:<20} {r['path'] or 'not found'}")

    # 3. AGENT CLIs
    add("\n[3] AGENT CLIs DETECTED")
    for r in report["agent_clis"]:
        ver = f"  [{r['version']}]" if r["version"] else ""
        add(f"    {_mark(r['found'])} {r['name']:<18} {r['path'] or 'not found'}{ver}")

    # 4. MCP WIRING
    add("\n[4] MCP WIRING (brain server registered?)")
    for r in report["mcp_wiring"]:
        if not r["config_exists"]:
            add(f"    - {r['agent']:<38} {r['detail']}")
        else:
            add(f"    {_mark(r['brain_registered'])} {r['agent']:<38} {r['detail']}")
            add(f"        {r['config']}")

    # 5. PROVIDER ACCOUNTS
    add("\n[5] PROVIDER ACCOUNTS")
    ps = report["providers"]
    if not ps.get("available"):
        add(f"    (providers module unavailable: {ps.get('error')})")
    else:
        add(f"    {ps['configured_count']}/{ps['catalog_size']} configured "
            f"({ps.get('free_configured_count', 0)} free)")
        for a in ps["accounts"]:
            add(f"    {_mark(bool(a['configured']))} {a['provider']:<12} "
                f"{a['billing']:<4} {a['free_type']:<14} "
                f"key: {a['key_source']:<26} {a['model']}")
            if "test" in a:
                t = a["test"]
                add(f"        test: {'ok' if t['ok'] else 'FAIL'} "
                    f"http={t['http']} {t['latency_ms']}ms "
                    f"models={t['model_count']} {t['error']}".rstrip())

    # 6. BRAIN STORE
    add("\n[6] BRAIN STORE")
    s = report["store"]
    add(f"    {_mark(s['brain_dir_exists'])} BRAIN_DIR        {s['brain_dir']}")
    add(f"      notes          {s['note_count']}")
    add(f"    {_mark(s['handoff_current_exists'])} handoff/current  "
        f"{'present' if s['handoff_current_exists'] else 'absent (no baton in flight)'}")
    strict = "present, strict" if s["handoff_schema_strict"] else (
        "present, NOT strict" if s["handoff_schema_exists"] else "absent")
    add(f"    {_mark(s['handoff_schema_exists'] and s['handoff_schema_strict'])} "
        f"schemas/handoff  {strict}")

    # 7. VERDICT
    v = report["verdict"]
    add("\n" + "=" * 64)
    add("  VERDICT — can this host run swarm tasks?")
    add("=" * 64)
    if v["can_run_swarm"]:
        add(f"    YES. Worker path(s) available: {'; '.join(v['available_paths'])}.")
        if not v["cli_worker_available"]:
            add("    No agent CLI is installed, but a provider account alone is a")
            add("    valid worker via the direct-API path.")
        elif not v["provider_worker_available"]:
            add("    No provider account configured, but an installed CLI can work.")
    else:
        add("    NO. This host has no worker: no agent CLI is installed and no")
        add("    provider account is configured.")
        add("    Fix it with either:")
        add(f"      {v['fix_command']}")
        add("      ...or install one of: cline, kiro-cli, agy, q, claude, codex, gemini")
    add("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Analyse whether this host can run shared-brain swarm tasks."
    )
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of the human report")
    ap.add_argument("--test-accounts", action="store_true",
                    help="also health-check each configured provider account "
                         "(free model-listing call, no tokens consumed)")
    args = ap.parse_args()

    report = build_report(test_accounts=args.test_accounts)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_human(report))

    # Exit non-zero ONLY on a genuine blocker: no worker path of any kind.
    # A missing IDE, an unregistered MCP config, or an absent handoff are all
    # informational, not failures.
    return 0 if report["verdict"]["can_run_swarm"] else 1


if __name__ == "__main__":
    sys.exit(main())
