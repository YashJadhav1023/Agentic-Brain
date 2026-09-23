"""Hermetic defaults for the whole test suite.

Installed once from ``tests/__init__.py``, so it runs before any test module is
imported, whether the suite is started with ``unittest discover -s tests -t .``
or a single module is run as ``python -m unittest tests.unit.test_x``.

Two pieces of live operator state used to leak into (and out of) the suite:

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

Tests that deliberately validate the *shipped* config file (for example "no secret
material in config/providers.json") keep reading the canonical path explicitly;
they are read-only.

Set ``BRAIN_TESTS_LIVE_CONFIG=1`` to opt out of the fixture and run against the
live configuration on purpose.
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


def install() -> None:
    """Pin hermetic config and auth defaults for this test process (idempotent)."""
    global _installed
    if _installed:
        return
    _installed = True

    if os.environ.get("BRAIN_TESTS_LIVE_CONFIG", "").strip() != "1":
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
    root = Path(tempfile.mkdtemp(prefix="brain-test-home-"))
    atexit.register(shutil.rmtree, root, True)

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
