"""Hermetic defaults for the whole test suite.

Installed once from ``tests/__init__.py``, so it runs before any test module is
imported, whether the suite is started with ``unittest discover -s tests -t .``
or a single module is run as ``python -m unittest tests.unit.test_x``.

Live operator state used to leak into (and out of) the suite:

``config/providers.json``
    The live provider/account configuration. The dashboard wizard writes every
    account an operator registers into it, so tests that asserted "exactly these
    accounts exist" broke as soon as a real account was added, and tests that
    drive the wizard wrote into (and rewrote the formatting of) the real file.
    The suite now reads a *copy* of ``tests/fixtures/providers.json`` selected with
    ``BRAIN_PROVIDERS_CONFIG``, which every config reader honours. The copy lives
    in a temporary directory, so a test that mutates configuration can corrupt
    neither the fixture nor the operator's file.

``runtime/mission_control.token``
    ``dashboard.get_or_create_auth_token()`` falls back to creating this file when
    ``MISSION_CONTROL_AUTH_TOKEN`` is unset. A per-run random test token is pinned
    in the environment instead, so starting a test server never creates or reads
    the operator's real bearer token. Authentication stays fully enforced; tests
    authenticate with this token exactly as the browser UI does with the real one.

It also (each with its own opt-out env var, see ``install``) points the default
credential store, keyring and account-profile roots at temp dirs, refuses real
agent CLI runs, redirects every repo-state writer (handoffs/, sessions/,
memory/store/, runtime/, tasks/) to a per-run temp root, and snapshots the
tracked state files so tests/unit/test_zz_repo_state_unchanged.py can fail
loudly if anything still writes the checkout.

Tests that deliberately validate the *shipped* config file (for example "no secret
material in config/providers.json") keep reading the canonical path explicitly;
they are read-only.

Set ``BRAIN_TESTS_LIVE_CONFIG=1`` to opt out of the fixture and run against the
live configuration on purpose. ``BRAIN_TESTS_KEEP_CONFIG=1`` keeps an already
set ``BRAIN_PROVIDERS_CONFIG`` (used by CLI child processes a test spawns).
"""
from __future__ import annotations

import atexit
import os
import secrets
import shutil
import tempfile
from pathlib import Path

FIXTURE_CONFIG = Path(__file__).resolve().parents[1] / "fixtures" / "providers.json"

#: Environment variables read by product code; names only, values are generated.
CONFIG_ENV = "BRAIN_PROVIDERS_CONFIG"
TOKEN_ENV = "MISSION_CONTROL_AUTH_TOKEN"

_installed = False

#: Per-run temp root, exported so child interpreters a test spawns (which import
#: ``tests`` too, see test_mission_control_universal) share this run's state.
RUN_ROOT_ENV = "BRAIN_TESTS_RUN_ROOT"


def _shared_run_dir(name: str) -> Path:
    inherited = os.environ.get(RUN_ROOT_ENV, "").strip()
    if inherited and Path(inherited).is_dir():
        root = Path(inherited)  # owned (and cleaned up) by the parent run
    else:
        root = Path(tempfile.mkdtemp(prefix="brain-test-run-"))
        atexit.register(shutil.rmtree, root, True)
        os.environ[RUN_ROOT_ENV] = str(root)
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def install() -> None:
    """Pin hermetic config and auth defaults for this test process (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    keep_config = os.environ.get("BRAIN_TESTS_KEEP_CONFIG", "").strip() == "1" and os.environ.get(CONFIG_ENV)
    if os.environ.get("BRAIN_TESTS_LIVE_CONFIG", "").strip() != "1" and not keep_config:
        tmp_dir = Path(tempfile.mkdtemp(prefix="brain-test-config-"))
        atexit.register(shutil.rmtree, tmp_dir, True)
        config_copy = tmp_dir / "providers.json"
        shutil.copyfile(FIXTURE_CONFIG, config_copy)
        os.environ[CONFIG_ENV] = str(config_copy)

    # Always a throwaway token: never the operator's env value or token file.
    os.environ[TOKEN_ENV] = "test-" + secrets.token_urlsafe(24)

    if os.environ.get("BRAIN_TESTS_LIVE_HOME_STATE", "").strip() != "1":
        _isolate_credentials_and_profiles()

    if os.environ.get("BRAIN_TESTS_ALLOW_AGENT_CLI", "").strip() != "1":
        _block_real_agent_cli_runs()

    # Snapshot before anything can write, then redirect repo-state writers.
    REPO_SENTINEL.update(snapshot_protected_repo_files())
    MUTABLE_TREE_SENTINEL.update(snapshot_mutable_repo_trees())
    if os.environ.get("BRAIN_TESTS_LIVE_REPO_STATE", "").strip() != "1":
        _redirect_repo_state()


# ---------------------------------------------------------------------------
# Repo state: handoffs/, sessions/, memory/store/, runtime/, tasks/
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Repo sub-trees that product code writes mutable state into by default.
MUTABLE_REPO_DIRS = (
    ("handoffs",),
    ("sessions",),
    ("memory", "store"),
    ("runtime",),
    ("tasks",),
    ("ui", "dashboard", "runtime"),
)

#: Tracked files the suite must never modify. Checked by
#: tests/unit/test_zz_repo_state_unchanged.py against the snapshot taken here.
PROTECTED_REPO_FILES = (
    "config/providers.json",
    "config/pricing.json",
    "handoffs/current.md",
    "handoffs/current.json",
)

#: relative path -> sha256 (or None if absent) at suite start.
REPO_SENTINEL: dict[str, str | None] = {}

#: Temp root that replaces the mutable repo sub-trees for this run.
STATE_ROOT: Path | None = None


def snapshot_protected_repo_files() -> dict[str, str | None]:
    import hashlib

    out: dict[str, str | None] = {}
    for rel in PROTECTED_REPO_FILES:
        p = REPO_ROOT / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


#: relative path -> (mtime_ns, size) of every file in the mutable sub-trees.
MUTABLE_TREE_SENTINEL: dict[str, tuple[int, int]] = {}


def snapshot_mutable_repo_trees() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for prefix in MUTABLE_REPO_DIRS:
        root = REPO_ROOT.joinpath(*prefix)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                out[str(full.relative_to(REPO_ROOT))] = (st.st_mtime_ns, st.st_size)
    return out


def remap_repo_path(value):
    """Map a path inside a mutable repo sub-tree to the per-run temp root."""
    if STATE_ROOT is None or isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        return value
    p = Path(value)
    absolute = (p if p.is_absolute() else Path.cwd() / p).resolve()
    try:
        rel = absolute.relative_to(REPO_ROOT)
    except ValueError:
        return value
    if any(rel.parts[: len(prefix)] == prefix for prefix in MUTABLE_REPO_DIRS):
        target = STATE_ROOT / rel
        return target if isinstance(value, Path) else str(target)
    return value


def _wrap_init(cls, defaults: dict[str, str]) -> None:
    """Remap path arguments of ``cls.__init__`` into STATE_ROOT.

    Any str/Path argument that points into a mutable repo sub-tree is remapped;
    a parameter listed in ``defaults`` that is left as None gets the remapped
    repo default, so the product's own fallback (``Path(__file__)...`` or a
    cwd-relative ``Path("runtime/...")``) is never reached.
    """
    import functools
    import inspect

    original = cls.__init__
    if getattr(original, "_brain_test_remap", False):
        return
    sig = inspect.signature(original)

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        bound = sig.bind_partial(self, *args, **kwargs)
        for name, value in list(bound.arguments.items()):
            if name != "self":
                bound.arguments[name] = remap_repo_path(value)
        for name, rel in defaults.items():
            if bound.arguments.get(name) is None:
                bound.arguments[name] = remap_repo_path(REPO_ROOT / rel)
        return original(*bound.args, **bound.kwargs)

    init._brain_test_remap = True  # type: ignore[attr-defined]
    cls.__init__ = init


def _redirect_repo_state() -> None:
    """Send every default repo-state writer to a per-run temp root.

    Unredirected, a full run rewrote the tracked handoffs/current.{md,json} and
    sessions/session_registry.json, appended to runtime/logs, runtime/audit and
    runtime/analytics, wrote runtime/jobs/*.json and runtime/sandboxes records,
    and grew memory/store/shared_memory.db in whatever checkout it ran in,
    including the operator's live one. Classes are patched before the dashboard
    module is imported, so its module-level singletons are redirected too.
    """
    global STATE_ROOT
    STATE_ROOT = _shared_run_dir("repo-state")

    # Env hooks the product already honours.
    os.environ["BRAIN_SANDBOX_ROOT"] = str(STATE_ROOT / "runtime" / "sandboxes")
    os.environ["BRAIN_DIR"] = str(STATE_ROOT / "agentic-brain")

    import importlib

    targets = (
        ("handoffs.handoff_manager", "HandoffManager", {"root_dir": "handoffs"}),
        ("sessions.session_manager", "SessionManager", {"registry_file": "sessions/session_registry.json"}),
        ("memory.store.memory_store", "MemoryStore", {"db_path": "memory/store/shared_memory.db"}),
        ("events.bus", "EventBus", {"log_path": "runtime/logs/events.jsonl"}),
        ("brain.orchestrator.job_manager", "JobManager", {}),
        ("brain.router.smart_router", "SmartRouter", {"history_file": "runtime/logs/routing_history.jsonl"}),
        ("brain.analytics.usage_tracker", "UsageTracker", {"storage_file": "runtime/analytics/usage.jsonl"}),
        ("brain.context.context_optimizer", "TokenTelemetryTracker", {"log_path": "runtime/logs/token_telemetry.jsonl"}),
        ("brain.governance.audit_logger", "AuditLogger", {"log_path": "runtime/audit/audit.jsonl"}),
        ("brain.analytics.performance_registry", "PerformanceRegistry", {"log_path": "runtime/analytics/performance.jsonl"}),
        ("brain.planner.planner", "Planner", {"storage_dir": "runtime/plans"}),
        ("brain.resources.ecc_federation", "ECCFederationManager", {"state_file": "runtime/external_capabilities.json"}),
        ("providers.registry.mcp_registry", "MCPRegistry", {}),
    )
    for module_name, class_name, defaults in targets:
        module = importlib.import_module(module_name)
        _wrap_init(getattr(module, class_name), defaults)

    # TaskManager: a None root also means "include the shared swarm pool", so
    # keep that meaning explicit while moving the root (swarm pool -> BRAIN_DIR).
    from tasks import manager as task_manager_module

    task_cls = task_manager_module.TaskManager
    original_task_init = task_cls.__init__
    if not getattr(original_task_init, "_brain_test_remap", False):

        def task_init(self, root_tasks_dir=None, include_swarm=None):
            if include_swarm is None:
                include_swarm = root_tasks_dir is None
            root = REPO_ROOT / "tasks" if root_tasks_dir is None else root_tasks_dir
            return original_task_init(self, remap_repo_path(root), include_swarm)

        task_init._brain_test_remap = True  # type: ignore[attr-defined]
        task_cls.__init__ = task_init


#: Agent CLIs whose real invocation runs a model on a real account.
AGENT_CLI_NAMES = frozenset({"agy", "antigravity", "antigravity-ide", "cline", "kiro-cli"})
#: Read-only probes that never reach a model (binary discovery uses these).
HARMLESS_ARGS = frozenset({"--version", "-v", "version", "--help", "-h"})


class AgentCliBlocked(FileNotFoundError):
    """Raised instead of spawning a real agent CLI from inside the test suite."""


def _block_real_agent_cli_runs() -> None:
    """Refuse to spawn a real agent CLI (``agy -p ...``, ``cline ...``) in tests.

    Several dashboard endpoints execute work in a background thread
    (``/api/dispatch``, ``/api/execute``, ``/api/jobs`` with the default
    ``auto_execute=True``). Before this guard, a test that only meant to check
    auth or rate limiting ran the live Antigravity CLI headless on a real
    account profile, spending real quota and writing real conversations. Tests
    that exercise command construction mock ``subprocess.run``/``Popen`` and
    never reach this guard. A blocked spawn surfaces as ``FileNotFoundError``,
    which adapters already treat as "CLI unavailable".
    """
    import subprocess

    original_init = subprocess.Popen.__init__
    if getattr(original_init, "_brain_test_guard", False):
        return

    def guarded_init(self, args, *pargs, **kwargs):
        argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or ())
        if argv and not kwargs.get("shell"):
            exe = os.path.basename(os.fsdecode(argv[0]))
            rest = {os.fsdecode(a) for a in argv[1:]}
            if exe in AGENT_CLI_NAMES and not (rest and rest <= HARMLESS_ARGS):
                raise AgentCliBlocked(
                    f"test suite refused to run the real agent CLI {exe!r}; mock "
                    "subprocess or set BRAIN_TESTS_ALLOW_AGENT_CLI=1 deliberately"
                )
        return original_init(self, args, *pargs, **kwargs)

    guarded_init._brain_test_guard = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = guarded_init  # type: ignore[method-assign]


class _DisabledKeyring:
    """Stand-in for KeyringCredentialStore that never reaches the OS keyring."""

    service = "mission-control-test"

    def is_available(self) -> bool:
        return False

    def store(self, key: str, secret: str) -> bool:
        return False

    def retrieve(self, key: str):
        return None

    def delete(self, key: str) -> bool:
        return False

    def exists(self, key: str) -> bool:
        return False


def _isolate_credentials_and_profiles() -> None:
    """Keep wizard/onboarding tests out of the operator's real home state.

    Driving the account wizard end to end used to (a) create a fresh
    ``~/.gemini/antigravity-account-<test-id>`` profile holding a fake token file
    on every run, (b) create ``~/.mission-control/cline/<test-id>`` dirs, and (c)
    store fake secrets in the real OS keyring and ``~/.config/agentic_brain``.
    These module-level defaults are redirected into a per-run temp root. Guards
    that *compare against* the real home (the IDE profile, the GUI) are untouched.
    """
    root = _shared_run_dir("home")

    from providers.registry import credential_manager as cm

    cm.DEFAULT_STORE_DIR = root / "agentic_brain"
    cm.DEFAULT_STORE_FILE = cm.DEFAULT_STORE_DIR / "credentials.dat"
    cm._DEFAULT_MANAGER = cm.CredentialManager(
        keyring_store=_DisabledKeyring(),  # type: ignore[arg-type]
        file_store=cm.EncryptedFileStore(cm.DEFAULT_STORE_FILE),
    )

    from agents.antigravity import auth as antigravity_auth

    antigravity_auth.DEFAULT_PROFILE_ROOT = root / "gemini"
    antigravity_auth.DEFAULT_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)

    from agents.cline import auth as cline_auth

    cline_auth.DEFAULT_CLINE_ROOT = root / "mission-control" / "cline"


def auth_headers() -> dict[str, str]:
    """The Authorization header the browser UI sends, using the test token."""
    return {"Authorization": f"Bearer {os.environ[TOKEN_ENV]}"}
