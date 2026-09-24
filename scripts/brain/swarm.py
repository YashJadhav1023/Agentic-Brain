#!/usr/bin/env python3
"""Ultra-Fast Autonomous Multi-Agent Swarm (Swarm Mesh) for Shared Brain.

Features:
1. Cognitive Task Classifier & Router: Analyzes tasks and assigns to the optimal agent
   (kiro-cli, cline, antigravity, antigravity-ide) based on capability heuristics.
2. Sub-Millisecond Queue & State Machine: Backed by ~/agentic-brain/swarm/tasks/.
3. Headless Parallel Worker Pool: Invokes headless-capable agents asynchronously.
4. Durable In-Editor Delivery Lifecycle: Cline and Antigravity IDE have no
   verified headless entrypoint, so tasks are durably delivered and only advance
   when the agent itself records an acknowledgement.
5. Inter-Agent Escalation Bus: Workers report blockers back to orchestrator for auto-unblocking.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()
SWARM_DIR = BRAIN_DIR / "swarm"
TASKS_DIR = SWARM_DIR / "tasks"
PENDING_DIR = TASKS_DIR / "pending"
IN_PROGRESS_DIR = TASKS_DIR / "in-progress"
COMPLETED_DIR = TASKS_DIR / "completed"
ESCALATED_DIR = TASKS_DIR / "escalated"
CLINE_DIR = SWARM_DIR / "cline"
CLINE_DELIVERIES_DIR = CLINE_DIR / "deliveries"
CLINE_TASK_FILE = CLINE_DIR / "CURRENT_TASK.md"

ANTIGRAVITY_IDE_DIR = SWARM_DIR / "antigravity-ide"
ANTIGRAVITY_IDE_DELIVERIES_DIR = ANTIGRAVITY_IDE_DIR / "deliveries"
ANTIGRAVITY_IDE_TASK_FILE = ANTIGRAVITY_IDE_DIR / "CURRENT_TASK.md"

# Agents that cannot be invoked headlessly. Antigravity IDE is an editor
# launcher with no prompt mode, so the swarm durably *delivers* a task to it and
# only reports a lifecycle state the agent itself acknowledged. Delivery is never
# treated as evidence of execution. Cline is deliberately NOT here: it ships a
# real headless CLI, so it is executed like Kiro and Antigravity.
DELIVERY_AGENTS = ("antigravity-ide",)

# A worker that is not an agent CLI at all: it calls a provider's API directly
# using a key the user registered. This is what lets the swarm run on a host
# where none of the agent CLIs or IDEs are installed.
API_AGENT = "api"

try:  # providers.py is optional so an older checkout still imports cleanly
    import providers as _providers
except Exception:  # pragma: no cover - degraded mode
    _providers = None


def _agent_cli_available(agent: str) -> bool:
    """Whether the headless CLI backing ``agent`` exists on this host."""
    if agent == API_AGENT:
        return bool(_api_fallback_provider())
    if agent == "kiro-cli":
        return KIRO_CLI_BIN.exists()
    if agent in ("antigravity", "antigravity-api"):
        return ANTIGRAVITY_BIN.exists()
    if agent == "cline":
        return CLINE_BIN.exists()
    if agent in DELIVERY_AGENTS:
        return True  # delivery agents are staged, not executed
    return False


def _api_fallback_provider() -> str | None:
    """The provider account to use when no agent CLI is available."""
    if _providers is None:
        return None
    try:
        forced = os.environ.get("BRAIN_SWARM_API_PROVIDER")
        if forced:
            key, _ = _providers.resolve_key(forced)
            return forced if key is not None else None
        return _providers.best_free_account()
    except Exception:
        return None


def _run_api_worker(task: dict, prompt: str) -> tuple[str, str, int]:
    """Execute a task by calling a provider API directly.

    Returns ``(output, error, exit_code)`` and records a full token/cost account
    on the task, because a provider reports real usage and guessing it would
    make the cost view fiction.
    """
    if _providers is None:
        return "", "providers module unavailable; cannot use an API account", 1
    provider = _api_fallback_provider()
    if not provider:
        return "", ("no provider account configured; add one with "
                    "`scripts/brain/brain providers add <provider>`"), 1

    result = _providers.chat_with_failover(
        prompt,
        system=("You are a headless worker in a multi-agent swarm. Complete the task "
                "and report concretely what you determined or changed. You have no "
                "tools, so do not claim to have run commands or edited files."),
        timeout=float(TASK_TIMEOUT_SECONDS),
        preferred=provider,
    )
    if not result.get("ok"):
        trail = "; ".join(
            f"{a['provider']}:{'quota' if a['capacity'] else 'error'}"
            for a in (result.get("attempts") or [])
        )
        return "", (f"{result.get('error', 'unknown provider error')}"
                    + (f" [tried {trail}]" if trail else "")), 1

    usage = result.get("usage") or {}
    task["token_usage"] = {
        "provider": result.get("provider"),
        "model": result.get("model"),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "billing": result.get("billing", "unknown"),
    }
    if result.get("failed_over"):
        # Record which account actually served the work; assuming the preferred
        # one would misattribute both the tokens and the cost.
        task["provider_attempts"] = result.get("attempts")
    task["model"] = result.get("model")
    task["worker_kind"] = "provider-api"
    return result.get("text", ""), "", 0


KIRO_CLI_BIN = Path(shutil.which("kiro-cli") or str(Path.home() / ".local/bin/kiro-cli"))
ANTIGRAVITY_BIN = Path(shutil.which("antigravity") or str(Path.home() / ".local/bin/antigravity"))
CLINE_BIN = Path(shutil.which("cline") or str(Path.home() / ".local/bin/cline"))
# Cline's own `cline` provider bills Cline Credits and that balance is $0.00, so
# every task routed there died mid-run. The free route is the Gemini provider,
# authenticated with the Gemini API key already attached for antigravity-api.
# Override with BRAIN_SWARM_CLINE_PROVIDER, or set it empty to use whatever
# provider cline has persisted.
CLINE_PROVIDER = os.environ.get("BRAIN_SWARM_CLINE_PROVIDER", "gemini").strip()

# ---------------------------------------------------------------------------
# Cline free-model enforcement
# ---------------------------------------------------------------------------
# Cline can reach several providers, and most of them bill. Observed on this host
# 2026-09-22: persisted state had drifted back to the paid `cline` provider on
# `moonshotai/kimi-k3`, with OpenRouter holding `anthropic/claude-fable-5`
# ($10/M in, $50/M out). The swarm passing `-P gemini` protected swarm runs only;
# any other entry point still used the paid route.
#
# Only the Gemini provider is a verified free route on this host. OpenRouter's
# `:free` slugs are no longer free ("This model is unavailable for free"), and the
# `cline` provider needs Cline Credits, which are $0.00.
CLINE_FREE_PROVIDER = "gemini"
# Gemini ids that are free-tier. Kept explicit rather than pattern-matched so a new
# paid Gemini tier cannot slip through by naming convention.
CLINE_FREE_MODELS = tuple(
    model.strip()
    for model in os.environ.get(
        "BRAIN_SWARM_CLINE_FREE_MODELS",
        "gemini-3.6-flash,gemini-3.6-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite",
    ).split(",")
    if model.strip()
)
CLINE_DEFAULT_FREE_MODEL = CLINE_FREE_MODELS[0] if CLINE_FREE_MODELS else "gemini-3.6-flash"
# Set to 0 to allow paid cline providers/models again.
CLINE_ENFORCE_FREE = os.environ.get("BRAIN_SWARM_CLINE_ENFORCE_FREE", "1") != "0"
CLINE_CONFIG_DIR = Path(os.environ.get("BRAIN_CLINE_CONFIG_DIR", Path.home() / ".cline"))
CLINE_GLOBAL_STATE = CLINE_CONFIG_DIR / "data" / "globalState.json"
CLINE_PROVIDERS_FILE = CLINE_CONFIG_DIR / "data" / "settings" / "providers.json"


def cline_free_model(requested: str = "") -> str:
    """The model to actually send to cline, forced onto the free tier.

    A requested model is honoured only if it is on the free allowlist; anything
    else, including an empty selection, becomes the default free model. Returning a
    concrete id rather than nothing matters because omitting `--model` lets cline
    fall back to its persisted choice, which is exactly the mutable state that
    drifted to a paid model.
    """
    if not CLINE_ENFORCE_FREE:
        return requested
    if requested and requested in CLINE_FREE_MODELS:
        return requested
    return CLINE_DEFAULT_FREE_MODEL


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def pin_cline_free_route() -> list[str]:
    """Force every cline mode and provider record onto the free route.

    Cline resolves its effective provider from `globalState.json`
    (`actModeApiProvider` / `planModeApiProvider`), not from `providers.json`, so
    both files must agree or a fix looks applied while the paid provider is still
    billed. Credentials are never read or rewritten here — only provider selection
    and model ids.

    Returns a list of human-readable changes, empty when already compliant.
    """
    if not CLINE_ENFORCE_FREE:
        return []
    changes: list[str] = []
    model = CLINE_DEFAULT_FREE_MODEL

    state = _load_json(CLINE_GLOBAL_STATE)
    if state:
        desired = {
            "actModeApiProvider": CLINE_FREE_PROVIDER,
            "planModeApiProvider": CLINE_FREE_PROVIDER,
            "actModeGeminiModelId": model,
            "planModeGeminiModelId": model,
        }
        for key, value in desired.items():
            if state.get(key) != value:
                changes.append(f"globalState.{key}: {state.get(key)!r} -> {value!r}")
                state[key] = value
        # Neutralise paid model ids parked on other providers so switching provider
        # by hand cannot silently select a billed model.
        for key in list(state):
            if key.endswith("ModelId") and key not in desired and state.get(key):
                changes.append(f"globalState.{key}: cleared {state[key]!r}")
                state[key] = ""
            if key.endswith("ModelInfo") and state.get(key):
                info = state[key]
                if isinstance(info, dict) and (info.get("inputPrice") or info.get("outputPrice")):
                    changes.append(f"globalState.{key}: cleared paid model info")
                    state[key] = {}
        if changes:
            _write_json_atomic(CLINE_GLOBAL_STATE, state)

    providers = _load_json(CLINE_PROVIDERS_FILE)
    if providers:
        provider_changes: list[str] = []
        if providers.get("lastUsedProvider") != CLINE_FREE_PROVIDER:
            provider_changes.append(
                f"providers.lastUsedProvider: {providers.get('lastUsedProvider')!r} -> {CLINE_FREE_PROVIDER!r}"
            )
            providers["lastUsedProvider"] = CLINE_FREE_PROVIDER
        blob = (providers.get("providers") or {}).get(CLINE_FREE_PROVIDER)
        if isinstance(blob, dict):
            settings = blob.setdefault("settings", {})
            if settings.get("model") != model:
                provider_changes.append(f"providers.{CLINE_FREE_PROVIDER}.model: {settings.get('model')!r} -> {model!r}")
                settings["model"] = model
        # Clear models parked on the billing providers. Selection is pinned to the
        # free provider, so these are inert today; clearing them means a manual
        # `-P cline` or `-P openrouter` cannot inherit a paid default either.
        for name, other in (providers.get("providers") or {}).items():
            if name == CLINE_FREE_PROVIDER or not isinstance(other, dict):
                continue
            other_settings = other.get("settings")
            if isinstance(other_settings, dict) and other_settings.get("model"):
                provider_changes.append(f"providers.{name}.model: cleared {other_settings['model']!r}")
                other_settings["model"] = ""
        if provider_changes:
            _write_json_atomic(CLINE_PROVIDERS_FILE, providers)
            changes.extend(provider_changes)
    return changes

# --- Antigravity capacity pools (multiple accounts) --------------------------
# Antigravity supports two authentication modes, and each mode is a separate
# account with its own quota, so the swarm treats them as a pool it can fail over
# between rather than as one agent.
#
#   oauth : a signed-in Google account. Its token lives in the OS keyring, and the
#           whole Antigravity model catalogue is available (Gemini, Claude, GPT).
#   api   : a Gemini API key. Per the official docs, requests then go *directly to
#           the Gemini API* and the CLI establishes no account session, so only
#           Gemini models are reachable and usage is billed to that key rather than
#           to an Antigravity seat.
#           https://antigravity.google/docs/cli/install#using-a-gemini-api-key
#
# API-key mode requires BOTH `"modelProvider": "gemini"` in the account's
# settings.json AND GEMINI_API_KEY in the environment; the key alone does nothing.
# Accounts are isolated with --app_data_dir, which selects a directory *name* under
# ~/.gemini, so a second account can never disturb the first one's session.
GEMINI_DATA_ROOT = Path(os.environ.get("BRAIN_GEMINI_DATA_ROOT", Path.home() / ".gemini")).resolve()
ANTIGRAVITY_OAUTH_DATA_DIR = os.environ.get("BRAIN_ANTIGRAVITY_OAUTH_DATA_DIR", "antigravity-cli")
ANTIGRAVITY_API_DATA_DIR = os.environ.get("BRAIN_ANTIGRAVITY_API_DATA_DIR", "antigravity-api")
# Additional OAuth accounts registered through the dashboard land in
# ~/.gemini/<prefix><name>. Discovering them turns each signed-in Google account
# into schedulable capacity instead of leaving it unused on disk. Set
# BRAIN_ANTIGRAVITY_MULTI_OAUTH=0 to use only the single configured primary.
OAUTH_ACCOUNT_PREFIX = os.environ.get("BRAIN_ANTIGRAVITY_ACCOUNT_PREFIX", "antigravity-account-")
MULTI_OAUTH_DISCOVERY = os.environ.get("BRAIN_ANTIGRAVITY_MULTI_OAUTH", "1") not in ("0", "false", "no")
# An abandoned registration leaves a near-empty directory behind. Scheduling one
# would send work to an account that cannot authenticate, so require a real
# credential payload before treating a directory as a usable account.
OAUTH_ACCOUNT_MIN_BYTES = int(os.environ.get("BRAIN_ANTIGRAVITY_ACCOUNT_MIN_BYTES", "100000"))
# The key is read from a file by preference. It is never accepted as a command
# line argument, because arguments are visible to any process via /proc and ps.
ANTIGRAVITY_API_KEY_FILE = Path(
    os.environ.get("BRAIN_ANTIGRAVITY_API_KEY_FILE", Path.home() / ".config/brain/antigravity-api.key")
)
ANTIGRAVITY_API_KEY_ENV = "BRAIN_ANTIGRAVITY_API_KEY"
# An API-key account reaches the Gemini API only, so Claude and GPT models must
# never be requested on it. This list is the set verified to actually answer on an
# API key: pro tiers and gemini-3.8-flash-high were probed and rejected with
# "Agent execution terminated due to error", so requesting them would look like an
# agent fault. Ordered best first: newer model, then higher effort. Override with a
# comma separated list if a key's entitlements differ.
ANTIGRAVITY_API_MODELS = tuple(
    model.strip()
    for model in os.environ.get(
        "BRAIN_ANTIGRAVITY_API_MODELS",
        "gemini-3.8-flash-medium,gemini-3.7-flash-high,gemini-3.7-flash-medium,"
        "gemini-3.8-flash-low,gemini-3.7-flash-low",
    ).split(",")
    if model.strip()
)
ANTIGRAVITY_MULTI_ACCOUNT = os.environ.get("BRAIN_ANTIGRAVITY_MULTI_ACCOUNT", "1") != "0"
# model_policy emits `--mode=plan` for tasks it classifies as planning work, which
# is correct for the plan-only orchestrator and wrong for a dispatched swarm task:
# Antigravity then drafts a plan and stops with "please approve and I'll write it",
# so the run reports success having changed nothing. A dispatched task is meant to
# execute, and the guarded worktree is what makes that safe. Set
# BRAIN_SWARM_PLAN_MODE=1 to restore plan mode.
SWARM_PLAN_MODE = os.environ.get("BRAIN_SWARM_PLAN_MODE", "0") == "1"
EXECUTION_MODE = "accept-edits"

# Errors that mean "this account is out of capacity", as opposed to a broken task.
# Only these trigger a failover to the next account; a genuine task failure must
# not silently burn every account.
_CAPACITY_ERROR_RE = re.compile(
    r"(quota|rate.?limit|resource[_ ]exhausted|too many requests|429|"
    r"exceeded your current|capacity|overloaded|try again later)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentAccount:
    """One authenticated capacity pool for a single agent."""

    name: str
    agent: str
    kind: str  # "oauth" or "api"
    data_dir: str
    models_allowed: tuple[str, ...] = ()  # empty means the full catalogue

    @property
    def settings_path(self) -> Path:
        return GEMINI_DATA_ROOT / self.data_dir / "settings.json"

    @property
    def token_file(self) -> Path:
        """The account's own on-disk credential, if it has one.

        This is the keyring library's file fallback, named `<product>-oauth-token`.
        Only an account that carries one can be pinned to its own identity.
        """
        return GEMINI_DATA_ROOT / self.data_dir / "antigravity-oauth-token"

    @property
    def has_own_token(self) -> bool:
        return self.token_file.is_file()


def _read_api_key() -> str:
    """Read the Gemini API key from its file, falling back to the environment."""
    try:
        if ANTIGRAVITY_API_KEY_FILE.is_file():
            key = ANTIGRAVITY_API_KEY_FILE.read_text(encoding="utf-8").strip()
            if key:
                return key
    except OSError:
        pass
    return os.environ.get(ANTIGRAVITY_API_KEY_ENV, "").strip()


def discovered_oauth_accounts() -> list[AgentAccount]:
    """Every signed-in Antigravity OAuth account present under ~/.gemini.

    Accounts are isolated by ``--app_data_dir``, so each authenticated directory
    is independent capacity with its own quota. Previously only one fixed
    directory name was ever used, so registering a second, third or fourth Google
    account through the dashboard had no effect: the extra accounts sat on disk
    and were never scheduled. Discovery makes them real workers.

    A directory counts as authenticated only if it carries a credential payload
    of meaningful size; the dashboard's registration wizard leaves small stub
    directories behind on abandoned attempts, and scheduling those would route
    tasks to an account that cannot answer.
    """
    accounts: list[AgentAccount] = []
    seen: set[str] = set()

    # The configured primary always comes first, whether or not it matches the
    # discovery heuristic, so an explicit override is never overruled.
    primary = ANTIGRAVITY_OAUTH_DATA_DIR
    accounts.append(AgentAccount("oauth", "antigravity", "oauth", primary))
    seen.add(primary)

    if not MULTI_OAUTH_DISCOVERY or not GEMINI_DATA_ROOT.exists():
        return accounts

    candidates: list[tuple[int, str]] = []
    for entry in sorted(GEMINI_DATA_ROOT.iterdir()):
        name = entry.name
        if not entry.is_dir() or name in seen:
            continue
        if not name.startswith(OAUTH_ACCOUNT_PREFIX):
            continue
        # The IDE keeps its own directories under the same root; it is a delivery
        # target with no headless mode, so it is not schedulable capacity.
        if "-ide" in name:
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        except OSError:
            continue
        if size >= OAUTH_ACCOUNT_MIN_BYTES:
            candidates.append((size, name))

    # Largest first is a reasonable proxy for the most-established session.
    for _size, name in sorted(candidates, reverse=True):
        accounts.append(AgentAccount(f"oauth:{name}", "antigravity", "oauth", name))
        seen.add(name)
    return accounts


def antigravity_accounts(for_agent: str = "antigravity") -> list[AgentAccount]:
    """Accounts to try, best first for the requesting agent.

    ``antigravity`` prefers the signed-in accounts because they serve every model.
    ``antigravity-api`` prefers its own key so the two run as independent workers
    at the same time; each still falls back to the other on a capacity error.
    Every additional OAuth account discovered on disk is extra capacity in the
    same failover chain.
    """
    oauth_accounts = discovered_oauth_accounts()
    api = AgentAccount("api-key", "antigravity-api", "api", ANTIGRAVITY_API_DATA_DIR, ANTIGRAVITY_API_MODELS)
    if for_agent == "antigravity-api":
        return [api, *oauth_accounts] if ANTIGRAVITY_MULTI_ACCOUNT else [api]
    return [*oauth_accounts, api] if ANTIGRAVITY_MULTI_ACCOUNT else [oauth_accounts[0]]


def token_expiry(account: AgentAccount) -> datetime | None:
    """Expiry of an account's own credential, or None if it cannot be determined.

    Read from the id_token's `exp` claim. Only the claim is parsed; no token value
    is logged or returned.
    """
    if not account.has_own_token:
        return None
    try:
        payload = json.loads(account.token_file.read_text(encoding="utf-8"))
        token = payload.get("id_token")
        if not isinstance(token, str) or token.count(".") != 2:
            return None
        claims = token.split(".")[1]
        claims += "=" * (-len(claims) % 4)
        exp = json.loads(base64.urlsafe_b64decode(claims)).get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, binascii.Error):
        return None


def account_available(account: AgentAccount) -> tuple[bool, str]:
    """Whether an account has usable credentials, without spending any quota."""
    if account.kind == "api":
        if not _read_api_key():
            return False, (
                f"no Gemini API key found; write it to {ANTIGRAVITY_API_KEY_FILE} "
                f"or export {ANTIGRAVITY_API_KEY_ENV}"
            )
        return True, "Gemini API key present"
    if not (GEMINI_DATA_ROOT / account.data_dir).is_dir():
        return False, f"no data directory for {account.data_dir}; run `antigravity` once to sign in"
    # An expired id_token is NOT evidence the account is unusable. The credential file
    # also carries an opaque `token` that the CLI refreshes and writes back, so a
    # profile whose id_token lapsed days ago still authenticates. Verified 2026-09-22:
    # antigravity-account-2077 (id_token expired 2026-09-09) and antigravity-account-3
    # (expired 2026-09-21) both answered as their own identities. An earlier version of
    # this function rejected them as "expired" and removed real capacity from the pool.
    if keyring_isolation_env(account):
        expiry = token_expiry(account)
        if expiry:
            return True, f"pinned to its own credential (id_token exp {expiry:%Y-%m-%d %H:%M}Z, refreshed on use)"
        return True, f"pinned to its own credential: {account.data_dir}"
    # The primary has no file credential; its session lives in the OS keyring, which
    # cannot be inspected without spending a request, so directory presence is the
    # only honest local signal.
    return True, f"signed-in account data directory present: {account.data_dir}"


# A dead path used to make the OS keyring unreachable for one child process.
# The variable must be SET to a bogus value, not unset: when it is merely unset the
# D-Bus client rebuilds the session bus address from XDG_RUNTIME_DIR and reaches
# gnome-keyring anyway. Verified 2026-09-22 — `env -u DBUS_SESSION_BUS_ADDRESS`
# still authenticated as the signed-in identity, while a bogus value authenticated
# as the profile's own account. Overriding XDG_RUNTIME_DIR as well is unnecessary
# and was dropped: pointing it at a missing directory breaks unrelated things in the
# child (socket paths, tmpfiles) for no gain.
_KEYRING_BLOCK_PATH = "/dev/null"
KEYRING_ISOLATION = os.environ.get("BRAIN_ANTIGRAVITY_KEYRING_ISOLATION", "1") != "0"


def keyring_isolation_env(account: AgentAccount) -> dict[str, str]:
    """Environment that pins an oauth account to its own on-disk credential.

    Antigravity resolves credentials through `ChainedAuth -> keyringAuth`. There is
    no separate file-based provider: the per-directory `antigravity-oauth-token` is
    the keyring library's *file fallback*, read only when the OS keyring cannot be
    reached. Because gnome-keyring runs under user@1000.service and holds whatever
    Google account signed in last, every data directory otherwise authenticates as
    that one identity — `--app_data_dir` isolates state, not credentials. Verified
    on 2026-09-22: five discovered "accounts" all resolved to the same email, so the
    multi-account pool was nominal rather than real capacity.

    Making the keyring unreachable forces the fallback and restores true isolation.
    Applied only to an account that actually carries its own token file; the primary
    signed-in directory has none and fails outright without the keyring, so it is
    always left alone.
    """
    if not KEYRING_ISOLATION or account.kind != "oauth":
        return {}
    if account.data_dir == ANTIGRAVITY_OAUTH_DATA_DIR or not account.has_own_token:
        return {}
    return {
        "DBUS_SESSION_BUS_ADDRESS": _KEYRING_BLOCK_PATH,
    }


def prepare_account(account: AgentAccount) -> tuple[dict[str, str], str]:
    """Return the extra environment for a run, creating settings only when needed.

    The signed-in account's configuration is never written to, so enabling the
    API-key account can never break an existing login.
    """
    if account.kind != "api":
        return keyring_isolation_env(account), ""
    key = _read_api_key()
    if not key:
        return {}, "no Gemini API key available"
    directory = GEMINI_DATA_ROOT / account.data_dir
    try:
        directory.mkdir(parents=True, exist_ok=True)
        settings_path = account.settings_path
        settings: dict = {}
        if settings_path.is_file():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                settings = {}
        if settings.get("modelProvider") != "gemini":
            settings["modelProvider"] = "gemini"
            settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        return {}, f"could not prepare {account.data_dir}: {error}"
    # Injected into the child environment only; never logged and never an argument.
    return {"GEMINI_API_KEY": key}, ""


def model_for_account(account: AgentAccount, selection) -> tuple[str, str, str]:
    """Constrain a model choice to what the account can actually serve.

    An API-key account talks to the Gemini API, so a Claude or GPT selection must
    be replaced rather than sent and failed. There is no stronger-tier equivalent
    to fall back to: pro models were probed on an API key and rejected, so the
    substitute is simply the best model the account is known to serve.
    """
    if selection is None:
        return "", "", ""
    model, effort, mode = selection.model, selection.effort or "", selection.mode or ""
    if mode == "plan" and not SWARM_PLAN_MODE:
        # Executing, not proposing. Plan mode makes the agent stop and ask for
        # approval, which reports success while changing nothing.
        mode = EXECUTION_MODE
    if account.models_allowed and model not in account.models_allowed:
        model = account.models_allowed[0]
        # The effort is encoded in the substituted model id, so a separate --effort
        # would either duplicate or contradict it.
        effort = ""
    return model, effort, mode


# How many models to try per account before giving up. The ranked fallback list
# can be long (14 entries for the signed-in Antigravity account), and each attempt
# can burn the full task timeout, so an uncapped walk turns one task into an
# hour-long serial retry. Four attempts is enough to clear a transient capacity
# wall without monopolising the queue.
MAX_MODEL_ATTEMPTS_PER_ACCOUNT = int(os.environ.get("BRAIN_SWARM_MAX_MODEL_ATTEMPTS", "4"))

_EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high)$")


def effort_encoded_in_model_id(model: str) -> bool:
    """Whether ``model`` already carries its effort level in its id.

    The Antigravity catalog marks the whole Gemini family as accepting
    ``--effort`` on the strength of one verified id, but the CLI only accepts the
    flag when it *matches* the suffix: `gemini-3.8-flash-low --effort=low` is
    fine, while `gemini-3.7-flash-medium --effort=low` is rejected outright with
    "conflicts with --effort=low". Observed on task-11cd2944, 2026-09-21, where a
    correct capacity downgrade then died on the carried-over effort.

    Treating an encoded suffix as "effort already specified" is the safe rule: the
    level is never lost, because it is part of the model id being sent.
    """
    return bool(_EFFORT_SUFFIX_RE.search(model or ""))


def model_supports_effort(agent: str, model: str) -> bool:
    """Whether ``model`` accepts a separate ``--effort`` flag on ``agent``.

    Needed when downgrading to a fallback: ``--effort`` is a hard error both on a
    model that does not accept it and on one whose id already encodes a different
    level, so the flag cannot simply be carried over from the original selection.
    """
    if effort_encoded_in_model_id(model):
        return False
    try:
        from model_policy import AGENT_CATALOGS, AgentTarget
    except ImportError:
        return False
    try:
        catalog = AGENT_CATALOGS[AgentTarget(agent)]
    except (ValueError, KeyError):
        return False
    return any(spec.name == model and spec.supports_effort for spec in catalog)


def capacity_fallback_plan(agent: str, account: AgentAccount, selection) -> list[tuple[str, str]]:
    """Ranked (model, effort) attempts for one account, best first.

    A capacity wall is specific to a model id, not to the account: an account out
    of capacity on `gemini-3.8-flash-medium` will still answer on
    `gemini-3.7-flash-medium`. Retrying only the same id across accounts therefore
    escalates tasks that a lower tier would have served, which is exactly what
    happened to the credential-audit task on 2026-09-21.

    Returns at least one entry so a selection-free call still runs the default model.
    """
    model, effort, _mode = model_for_account(account, selection)
    if not model_supports_effort(agent, model):
        effort = ""
    plan: list[tuple[str, str]] = [(model, effort)]
    if selection is None:
        return plan

    allowed = account.models_allowed
    for candidate in getattr(selection, "fallbacks", ()) or ():
        if len(plan) >= MAX_MODEL_ATTEMPTS_PER_ACCOUNT:
            break
        if allowed and candidate not in allowed:
            continue
        if any(candidate == already for already, _ in plan):
            continue
        candidate_effort = effort if model_supports_effort(agent, candidate) else ""
        plan.append((candidate, candidate_effort))
    return plan


def is_capacity_error(text: str) -> bool:
    return bool(_CAPACITY_ERROR_RE.search(text or ""))


# Antigravity's catch-all failure. Observed for a model the key is not entitled to
# and for a transient failure under concurrency, with no further detail on stdout
# or stderr, so it says nothing about whether the *task* is sound. Treated as
# retryable on another account rather than escalated, because escalating a task the
# other account could have served is the worse error.
_OPAQUE_ERROR_RE = re.compile(r"agent execution terminated due to error", re.IGNORECASE)


def is_opaque_error(text: str) -> bool:
    return bool(_OPAQUE_ERROR_RE.search(text or ""))


# An account whose own credential is rejected or has lapsed. This is a property of
# the account, not of the task, so it must fail over rather than escalate. Relevant
# now that non-primary accounts are pinned to their own token and can no longer
# silently borrow the keyring session.
_AUTH_ERROR_RE = re.compile(
    r"(authentication (failed|timed out)|failed to load stored token|"
    r"no ID token stored|token (is )?expired|invalid[_ ]grant|unauthenticated)",
    re.IGNORECASE,
)


def is_auth_error(text: str) -> bool:
    return bool(_AUTH_ERROR_RE.search(text or ""))


def should_try_next_account(text: str) -> bool:
    """Whether a failure justifies spending another account on the same task."""
    return is_capacity_error(text) or is_opaque_error(text) or is_auth_error(text)


# Round robin cursor used to spread Antigravity work across attached accounts.
_ANTIGRAVITY_ROTATION = [0]


def antigravity_pool() -> list[str]:
    """Antigravity agents that can actually run right now, best first."""
    if not ANTIGRAVITY_BIN.exists():
        return []
    pool = ["antigravity"]
    if ANTIGRAVITY_MULTI_ACCOUNT and _read_api_key():
        pool.append("antigravity-api")
    return pool


def antigravity_capacity() -> dict:
    """How much independent Antigravity capacity this host actually has.

    The agent *names* are a routing label; the real capacity is the number of
    separately authenticated accounts behind them, each with its own quota. These
    were previously invisible, so four registered Google accounts looked like one.
    """
    accounts = antigravity_accounts("antigravity")
    usable = []
    unusable = []
    for account in accounts:
        ok, basis = account_available(account)
        row = {"id": account.name, "kind": account.kind,
               "data_dir": account.data_dir, "basis": basis}
        (usable if ok else unusable).append(row)
    return {
        "oauth_accounts": sum(1 for a in accounts if a.kind == "oauth"),
        "api_accounts": sum(1 for a in accounts if a.kind == "api"),
        "usable": usable,
        "unusable": unusable,
        "total_usable": len(usable),
        "discovery_enabled": MULTI_OAUTH_DISCOVERY,
    }


def antigravity_assignment(agent: str, request=None) -> tuple[str, tuple[str, ...], str]:
    """Where ``create_task`` sends Antigravity-class work, without side effects.

    Returns ``(agent, accounts, note)``. ``accounts`` names every account the task
    can be assigned to: one entry when the assignment is fixed (pinned, or only one
    account attached), several when low/medium work alternates across the pool and
    the account is only decided at dispatch. The planner uses this so a plan shows
    exactly what dispatch will do; ``balance_antigravity`` adds the rotation.

    ``request`` is the task's ``SelectionRequest``. When it needs the strongest
    tier (high complexity or high/critical risk) the task is pinned to the
    signed-in ``antigravity`` pool, which serves every model. The API-key account
    is Gemini-flash only, so balancing hard work onto it silently capped it at a
    flash model. It still serves such a task as capacity failover (see
    ``antigravity_accounts``), and a weaker model served that way is recorded by
    ``record_model_downgrade``.
    """
    if agent not in ("antigravity", "antigravity-api"):
        return agent, (agent,), ""
    if request is not None and _needs_strongest_tier(request):
        return "antigravity", ("antigravity",), (
            "pinned to the signed-in Antigravity pool: needs the strongest tier "
            f"({request.complexity.value} complexity, {request.risk.value} risk); "
            "antigravity-api is capacity failover only"
        )
    pool = antigravity_pool()
    if not pool:
        return agent, (agent,), ""
    if len(pool) == 1:
        return pool[0], (pool[0],), "only one Antigravity account attached"
    return agent, tuple(pool), f"balanced across {len(pool)} Antigravity accounts"


def balance_antigravity(agent: str, request=None) -> tuple[str, str]:
    """Spread Antigravity-class work across every attached account.

    Both accounts are real headless workers, and the swarm runs tasks in parallel,
    so alternating assignment lets them work *at the same time*. Without this the
    higher weighted account would win every task and the second would only ever be
    used once the first was exhausted. Only low and medium work alternates; see
    ``antigravity_assignment`` for the pinning rule.
    """
    assigned, accounts, note = antigravity_assignment(agent, request)
    if len(accounts) <= 1:
        # Pinned work does not advance the cursor, so low/medium work keeps alternating.
        return assigned, note
    index = _ANTIGRAVITY_ROTATION[0] % len(accounts)
    _ANTIGRAVITY_ROTATION[0] += 1
    return accounts[index], note


def _needs_strongest_tier(request) -> bool:
    try:
        from .model_policy import needs_strongest_tier  # type: ignore[attr-defined]
    except ImportError:
        from model_policy import needs_strongest_tier  # type: ignore[no-redef]
    return needs_strongest_tier(request)


def _antigravity_model_strengths() -> dict[str, int]:
    """Capability rank of every id either Antigravity account can serve."""
    try:
        from .model_policy import ANTIGRAVITY_API_CATALOG, ANTIGRAVITY_CATALOG  # type: ignore[attr-defined]
    except ImportError:
        from model_policy import ANTIGRAVITY_API_CATALOG, ANTIGRAVITY_CATALOG  # type: ignore[no-redef]
    strengths: dict[str, int] = {}
    for spec in (*ANTIGRAVITY_CATALOG, *ANTIGRAVITY_API_CATALOG):
        strengths[spec.name] = max(strengths.get(spec.name, 0), spec.strength)
    return strengths


def record_model_downgrade(task: dict, selection) -> None:
    """Note on ``task`` when failover substituted a weaker model than was selected.

    Capacity failover can move a task to the Gemini-flash-only API account or walk
    down the fallback chain. Either is the right call over failing the task, but a
    task planned for a frontier model must not look as if it ran on one. The note
    says "served" only when the weaker model's attempt succeeded; otherwise it was
    the last model tried before the task failed.
    """
    if selection is None:
        return
    planned = getattr(selection, "model", "") or ""
    served = task.get("model") or ""
    if not planned or not served or served == planned:
        return
    strengths = _antigravity_model_strengths()
    before, after = strengths.get(planned), strengths.get(served)
    if before is not None and after is not None and after >= before:
        return
    account = task.get("antigravity_account") or "another account"
    attempts = task.get("antigravity_attempts") or []
    succeeded = bool(attempts) and ": ran on " in attempts[-1]
    outcome = "served" if succeeded else "last attempted (no attempt succeeded)"
    task["downgraded"] = (
        f"downgraded: selected {planned} (strength {before}), {outcome} {served} "
        f"(strength {after}) on {account} by capacity failover"
    )


# --- Execution sandbox -------------------------------------------------------
# Headless agents run with every approval gate disabled: kiro-cli with
# --trust-all-tools, antigravity with --dangerously-skip-permissions and cline
# with --auto-approve true. They therefore never run directly in a working tree.
# Each task gets a git worktree on its own throwaway branch, so an agent can
# write freely while the user's checkout is untouched and every change is
# reviewable as a branch and revertable by deleting it.
#
# The workspace root is a polyrepo: it holds one git repository per service and
# is NOT itself a repository, so the sandbox is always scoped to exactly one
# resolved repository.
WORKSPACE_ROOT = Path(os.environ.get("BRAIN_WORKSPACE_ROOT", Path(__file__).resolve().parents[2])).resolve()
# Kept outside the workspace so an agent never sees its own sandbox as project
# content and so the workspace stays clean.
SANDBOX_ROOT = Path(os.environ.get("BRAIN_SANDBOX_ROOT", Path.home() / ".cache/brain/sandboxes")).resolve()
SANDBOX_BRANCH_PREFIX = os.environ.get("BRAIN_SANDBOX_BRANCH_PREFIX", "brain/swarm")
# Used when a task names no repository. Empty means "run without repo access".
DEFAULT_SANDBOX_REPO = os.environ.get("BRAIN_SWARM_DEFAULT_REPO", "").strip()
SANDBOX_ENABLED = os.environ.get("BRAIN_SWARM_SANDBOX", "1") != "0"
# Phrases that mean the work deliberately spans more than one repository. A
# single worktree cannot guard that, so such a task is escalated rather than
# half-executed against one arbitrary service.
_CROSS_REPO_PHRASES = (
    "every service", "all services", "each service", "across services",
    "every repo", "all repos", "every repository", "all repositories",
    "across repos", "across repositories", "monorepo", "whole workspace",
    "entire workspace", "all microservices", "every microservice",
)


def _git(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def is_git_repository(path: Path) -> bool:
    """A real repository, not a skeleton. The workspace root has a partial .git
    directory with no objects or refs, and git rightly rejects it, so presence of
    .git alone is not enough."""
    if not (path / ".git").exists():
        return False
    try:
        proc = _git(["rev-parse", "--git-dir"], path, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def git_repositories(root: Path | None = None) -> list[Path]:
    """Every immediate child of the workspace that is a usable git repository."""
    base = root or WORKSPACE_ROOT
    if not base.is_dir():
        return []
    found = [child for child in sorted(base.iterdir()) if child.is_dir() and is_git_repository(child)]
    if is_git_repository(base):
        found.insert(0, base)
    return found


def _repo_aliases(repo: Path) -> set[str]:
    """Names a task might use for a repository, longest first when matched."""
    name = repo.name.lower()
    aliases = {name}
    for prefix in ("agentic-os-", "agentic_os_", "agentic-"):
        if name.startswith(prefix) and len(name) > len(prefix):
            aliases.add(name[len(prefix):])
    return aliases


def resolve_target_repo(text: str, explicit: str = "") -> tuple[Path | None, str]:
    """Pick the one repository a task targets.

    Returns (repo, reason). A None repo with a reason starting "ambiguous" means
    the caller must escalate rather than guess.
    """
    repos = git_repositories()
    if explicit:
        wanted = explicit.strip().lower()
        for repo in repos:
            if wanted == repo.name.lower() or wanted in _repo_aliases(repo):
                return repo, f"explicitly targeted: {repo.name}"
        known = ", ".join(repo.name for repo in repos) or "none discovered"
        return None, f"unknown repository '{explicit}'; known repositories: {known}"

    lowered = (text or "").lower()
    crossing = [phrase for phrase in _CROSS_REPO_PHRASES if phrase in lowered]
    matched = sorted(
        {repo for repo in repos if any(re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", lowered) for a in _repo_aliases(repo))},
        key=lambda repo: repo.name,
    )
    if crossing and len(matched) != 1:
        return None, (
            f"ambiguous: the task spans multiple repositories ({', '.join(crossing)}) and a single "
            "sandbox cannot guard that. Re-dispatch once per repository with --repo <name>."
        )
    if len(matched) == 1:
        return matched[0], f"resolved from the task text: {matched[0].name}"
    if len(matched) > 1:
        return None, (
            f"ambiguous: the task names {len(matched)} repositories ({', '.join(r.name for r in matched)}). "
            "Re-dispatch with --repo <name>."
        )
    if DEFAULT_SANDBOX_REPO:
        for repo in repos:
            if DEFAULT_SANDBOX_REPO.lower() in _repo_aliases(repo) or DEFAULT_SANDBOX_REPO.lower() == repo.name.lower():
                return repo, f"BRAIN_SWARM_DEFAULT_REPO={repo.name}"
    return None, "no repository named in the task; running without repository access"


def sandbox_branch(task_id: str) -> str:
    return f"{SANDBOX_BRANCH_PREFIX}/{task_id}"


def create_sandbox(task_id: str, repo: Path) -> tuple[dict | None, str]:
    """Check the repository out into a throwaway worktree on its own branch."""
    branch = sandbox_branch(task_id)
    path = SANDBOX_ROOT / task_id
    try:
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        if path.exists():
            # A crashed or requeued run left its worktree behind. Snapshot any
            # uncommitted edits onto its branch first, so reclaiming the path
            # never throws away an agent's work.
            finalize_sandbox({"repo": str(repo), "repo_name": repo.name, "path": str(path), "branch": branch})
            _git(["worktree", "remove", "--force", str(path)], repo)
            shutil.rmtree(path, ignore_errors=True)
        _git(["worktree", "prune"], repo)
        # A leftover branch from an earlier attempt must not block the sandbox,
        # but a branch carrying commits is reviewable work (finalize_sandbox
        # keeps partial changes "even on failure"), so it is renamed aside
        # rather than deleted. A branch with nothing new is simply dropped.
        if _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo).returncode == 0:
            ahead = _git(["rev-list", "--count", f"HEAD..refs/heads/{branch}"], repo).stdout.strip()
            if ahead not in ("", "0"):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                _git(["branch", "-m", branch, f"{branch}-prev-{stamp}"], repo)
            else:
                _git(["branch", "-D", branch], repo)
        proc = _git(["worktree", "add", "-b", branch, str(path), "HEAD"], repo)
    except (OSError, subprocess.SubprocessError) as error:
        return None, f"sandbox could not be created: {error}"
    if proc.returncode != 0:
        return None, f"sandbox could not be created: {(proc.stderr or proc.stdout).strip()[:300]}"
    base = _git(["rev-parse", "HEAD"], path)
    return {
        "repo": str(repo),
        "repo_name": repo.name,
        "path": str(path),
        "branch": branch,
        "base_commit": base.stdout.strip(),
    }, f"sandbox on {branch} in {repo.name}"


def finalize_sandbox(sandbox: dict) -> dict:
    """Commit whatever the agent changed onto the sandbox branch and tear down the
    worktree. The branch survives so the change is reviewable and revertable; a
    run that changed nothing leaves no branch behind."""
    repo = Path(sandbox["repo"])
    path = Path(sandbox["path"])
    branch = sandbox["branch"]
    result = {"branch": branch, "repo": sandbox["repo_name"], "changed": False, "commit": None, "diffstat": "", "files": 0}
    if not path.exists():
        return result
    if not (path / ".git").is_file():
        # A linked worktree always has a .git *file*. Without it git would walk
        # up from `path` and could stage and commit into whatever repository
        # happens to enclose the sandbox root, so refuse to touch it.
        result["error"] = f"{path} is not a git worktree; left untouched"
        return result
    try:
        _git(["add", "-A"], path)
        staged = _git(["diff", "--cached", "--name-only"], path)
        names = [line for line in staged.stdout.splitlines() if line.strip()]
        if names:
            # Identity is passed explicitly so a repository without local config
            # cannot fail the commit, and hooks are skipped: this is a sandbox
            # snapshot for review, not a contribution.
            commit = _git([
                "-c", "user.name=Shared Brain Swarm",
                "-c", "user.email=swarm@localhost",
                "commit", "--no-verify", "-m",
                f"brain/swarm sandbox: {branch.rsplit('/', 1)[-1]}",
            ], path)
            if commit.returncode == 0:
                head = _git(["rev-parse", "HEAD"], path)
                stat = _git(["show", "--stat", "--oneline", "HEAD"], path)
                result.update({
                    "changed": True,
                    "commit": head.stdout.strip(),
                    "diffstat": stat.stdout.strip()[:1200],
                    "files": len(names),
                })
            else:
                result["error"] = (commit.stderr or commit.stdout).strip()[:300]
        _git(["worktree", "remove", "--force", str(path)], repo)
        if not result["changed"]:
            # Nothing to review, so do not leave a branch lying around.
            _git(["branch", "-D", branch], repo)
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = str(error)[:300]
    shutil.rmtree(path, ignore_errors=True)
    return result


# Ensure queue directory structure exists
for d in [
    PENDING_DIR,
    IN_PROGRESS_DIR,
    COMPLETED_DIR,
    ESCALATED_DIR,
    CLINE_DIR,
    CLINE_DELIVERIES_DIR,
    ANTIGRAVITY_IDE_DIR,
    ANTIGRAVITY_IDE_DELIVERIES_DIR,
]:
    d.mkdir(parents=True, exist_ok=True)


# Verifiable installation markers for in-editor agents. Presence proves the agent
# exists on this machine; it never proves the agent ran. Lifecycle evidence is
# still the only thing that can show work happening.
DELIVERY_PRESENCE: dict[str, tuple[Path, ...]] = {
    "antigravity-ide": (
        Path("/usr/bin/antigravity-ide"),
        Path.home() / ".gemini/antigravity-ide",
    ),
}
DELIVERY_PRESENCE_GLOBS: dict[str, str] = {}


def delivery_agent_installed(agent: str) -> tuple[bool, str]:
    """Report whether an in-editor agent is installed, with the evidence used."""
    markers = DELIVERY_PRESENCE.get(agent, ())
    pattern = DELIVERY_PRESENCE_GLOBS.get(agent)
    for marker in markers:
        try:
            if pattern:
                matches = sorted(marker.glob(pattern)) if marker.is_dir() else []
                if matches:
                    return True, f"installed: {matches[-1].name}"
            elif marker.exists():
                return True, f"installed: {marker}"
        except OSError:
            continue
    return False, "not installed on this machine"


# ==============================================================================
# 1. Cognitive Task Classifier & Router
# ==============================================================================
#
# The router reads a task the way a dispatcher would, in three layers:
#
#   1. Explicit opt-in. antigravity-ide is chosen only when the task names it as
#      the target ("use antigravity-ide to ...", "send to antigravity-ide: ...").
#      It is delivery-only, so a keyword win would mean the task never runs.
#   2. Intent. The leading verb of each clause says what the task *asks for*:
#      "Run/Deploy/Restart/kubectl ..." is execution (kiro-cli), "Explain/Why/
#      Design/Compare ..." is reasoning (antigravity), "Fix/Implement/Add ..." is a
#      code edit (cline). A multi-file scope ("across every file", "every import",
#      "into separate modules") turns a code edit into antigravity work.
#   3. Vocabulary. Domain nouns in AGENT_PROFILES add +1 each and settle the cases
#      the verbs leave open ("Audit the AKS cluster node pool quota").
#
# Every signal is an additive weight, so the winner is the highest total and the
# confidence is the *margin* over the runner-up, not the raw top score. Ties are
# broken by the intent of the leading clause, never by dict order.
#
# antigravity-api has no vocabulary on purpose: it is the same class of worker as
# antigravity, and balance_antigravity() spreads antigravity-class work across
# both accounts. Letting a keyword ("summarize") pick it bypassed that balancing.
AGENT_PROFILES = {
    "kiro-cli": {
        "title": "Kiro CLI (Terminal & Cloud Workhorse)",
        # Infrastructure and runtime nouns. The tools themselves (kubectl, az,
        # npm, ...) are scored separately and more heavily; see _OPS_COMMAND_RE.
        "keywords": [
            "pod", "pods", "node pool", "node pools", "nodepool", "cluster",
            "clusters", "namespace", "namespaces", "container", "containers",
            "image", "images", "registry", "volume", "volumes", "replica",
            "replicas", "deployment", "rollout", "chart", "helm release",
            "logs", "log", "journal", "service", "services", "daemon", "process",
            "processes", "port",
            "disk", "disk usage", "venv", "virtualenv", "dependencies", "quota",
            "aks", "acr", "kubeconfig", "tfvars", "resource group",
            "storage account", "storage accounts", "subscription", "key vault",
            "vm", "vms", "host", "build agent", "runner", "ci", "pipeline",
            "environment", "dev", "uat", "prod", "staging", "production",
            "ingress", "dns", "load balancer", "firewall", "certificate",
            "certificates", "cron job", "kubernetes", "k8s", "terminal",
            "metrics", "alert rule", "kernel", "filesystem", "backup", "backups",
        ],
    },
    "cline": {
        # Cline runs headlessly and is the focused-coding agent: work contained to
        # one or a few files. These nouns are evidence that a task is a code edit.
        # Test *writing* is scored by _TEST_WRITING_RE so that "Run the unit
        # tests" is never mistaken for it.
        "title": "Cline (Headless Focused-Coding CLI)",
        "keywords": [
            # Frontend and styling
            "frontend", "css", "tailwind", "ui", "button", "buttons", "layout",
            "react", "component", "style", "styles", "styling", "page", "modal",
            "html", "tsx", "jsx", "vue", "drawer", "canvas", "header", "navbar",
            "sidebar", "footer", "card", "spinner", "responsive", "padding",
            "margin", "colors", "colours", "font", "theme", "dark mode", "grid",
            "flexbox", "form", "dropdown", "tooltip", "icon", "animation",
            "mobile",
            # Code units
            "function", "functions", "method", "methods", "class", "helper",
            "helpers", "handler", "handlers", "endpoint", "parser", "regex",
            "validation", "variable", "argument", "argument parser", "flag",
            "decorator", "hook", "props", "import", "imports", "type hints",
            "type hint", "type annotations", "annotations", "docstring",
            "docstrings", "jsdoc", "comment", "comments", "assertion",
            "error message", "algorithm", "lint", "prettier",
            # Bugs
            "bug", "bugs", "crash", "typo", "off-by-one", "null", "null pointer",
            "exception", "regression", "broken",
        ],
    },
    "antigravity": {
        "title": "Antigravity (Master Architect, Reasoning & Multi-File Synthesis)",
        "keywords": [
            "architecture", "architectural", "design", "protocol", "decision",
            "adr", "schema", "strategy", "policy", "governance", "trade-off",
            "trade-offs", "tradeoff", "tradeoffs", "escalation", "escalated",
            "escalate", "orchestrate", "orchestration", "synthesize", "security",
            "posture", "threat model", "risk", "risks", "migration", "roadmap",
            "rationale", "audit", "review", "investigate", "multi-step",
            "codebase", "polyrepo", "monorepo", "microservice", "microservices",
            "isms", "compliance", "implications", "options", "approach",
            "pros and cons",
        ],
    },
    "antigravity-api": {
        "title": "Antigravity (second account, Gemini API key)",
        # Reached only through balance_antigravity(), never by keyword.
        "keywords": [],
    },
    "antigravity-ide": {
        "title": "Antigravity IDE (In-Editor, Human-Driven)",
        # Opt-in only. This agent cannot be executed headlessly: the swarm can
        # only *deliver* to it and wait for a human to drive its Agent panel. It
        # must therefore never win a task by accident, or that task simply never
        # runs. Ask for it by name (see _IDE_TARGET_RE), or use --agent
        # antigravity-ide. "walk me through" and "in the editor" are ordinary
        # reasoning/editing phrases, not a request for the IDE.
        "keywords": [],
    },
}

# --- Signal weights ------------------------------------------------------------
_W_OPS_LEAD = 9.0      # task leads with a strong ops verb or a CLI tool
_W_OPS_ACTION = 7.0    # later imperative ops clause, or weak ops verb + ops context
_W_WEAK_VERB = 1.5     # a verb whose target gives no supporting evidence
_W_OPS_COMMAND = 2.0   # each distinct CLI tool named (kubectl, az, npm, ...)
_W_SHELL_SCRIPT = 4.0  # "bash script", "cron job": kiro writes and runs these
_W_REASONING = 6.0     # task leads with a question/explanation/design verb
_W_MULTI_FILE = 8.0    # code work whose scope spans many files
_W_CODE_VERB = 3.0     # edit verb with code evidence (file, function, bug, ...)
_W_FILE_TARGET = 2.0   # a named source file
_W_TEST_WRITING = 2.0  # "add/write ... tests"
_W_VISUAL_DESIGN = 7.0 # "Design the CSS grid layout" is styling, not architecture
_W_KEYWORD = 1.0       # each distinct profile keyword

# Always an execution request when a clause starts with one of these.
_STRONG_OPS_VERBS = frozenset({
    "run", "rerun", "re-run", "execute", "exec", "deploy", "redeploy", "restart",
    "reboot", "tail", "grep", "install", "reinstall", "uninstall", "scale",
    "rollback", "roll back", "rotate", "destroy", "provision", "push", "publish",
    "purge", "prune", "kill", "ssh", "curl", "ping", "cordon", "drain",
    "spin up", "tear down", "back up", "backup",
})
# Execution only when the task also names ops tools or nouns ("Apply the
# terraform plan" yes, "Apply a cross-file rename" no).
_WEAK_OPS_VERBS = frozenset({
    "apply", "delete", "remove", "build", "rebuild", "check", "list", "show",
    "scan", "inspect", "verify", "monitor", "get", "fetch", "move", "copy",
    "clean", "clean up", "cleanup", "upgrade", "downgrade", "start", "stop",
    "enable", "disable", "create", "set", "configure", "download", "upload",
    "sync", "count", "measure", "find", "test",
})
_CODE_VERBS = frozenset({
    "fix", "implement", "add", "write", "rename", "change", "make", "correct",
    "update", "refactor", "style", "restyle", "center", "centre", "align",
    "annotate", "document", "remove", "delete", "create", "build", "convert",
    "rewrite", "simplify", "optimize", "optimise", "tidy", "format", "replace",
    "split", "extract", "move", "patch", "modify", "edit", "adjust", "tweak",
    "hide", "show", "set", "start", "stop", "handle", "debug", "clean",
    "clean up", "cleanup", "inline", "memoize", "wrap", "collapse", "disable",
    "enable", "support", "validate", "sort", "parse", "return", "throw",
})
# Verbs that restructure a codebase by definition.
_MULTI_FILE_VERBS = frozenset({
    "restructure", "reorganize", "reorganise", "modularize", "modularise",
    "rearchitect", "re-architect",
})
_REASONING_VERBS = frozenset({
    "explain", "describe", "compare", "contrast", "summarize", "summarise",
    "outline", "propose", "plan", "design", "redesign", "draft", "evaluate",
    "assess", "analyze", "analyse", "investigate", "diagnose", "review",
    "recommend", "weigh", "justify", "decide", "discuss", "brainstorm",
    "research", "architect", "define", "clarify", "critique", "advise", "suggest",
    "estimate", "state", "consider", "reason", "think", "prioritize",
    "prioritise", "rank", "choose", "elaborate", "interpret", "predict",
    "walk me through", "give overview", "show me", "tell me", "help me",
    "write adr",
})
# Always a question, wherever it appears as the lead word.
_WH_WORDS = frozenset({"what", "why", "how", "which", "when", "where", "who", "whom", "whose", "should"})
# A question only when the sentence also ends with "?" ("Do a docker build" is not).
_AUX_WORDS = frozenset({"is", "are", "was", "were", "do", "does", "did", "will", "shall", "can", "could", "would", "may", "might", "must", "has", "have"})

# Politeness and sequencing words that precede the real verb.
_FILLER_RE = re.compile(
    r"^(?:(?:please|kindly|now|also|then|next|first|finally|quickly|just|ok|okay|so)\b[\s,]*"
    r"|(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|(?:i\s+(?:need|want|would\s+like)\s+you\s+to|go\s+ahead\s+and|let's|lets)\s+)+"
)
# "Use az to list ..." -> the real verb is "list" (the tool is still scored).
_USE_TOOL_RE = re.compile(r"^(?:use|using)\s+(?:the\s+)?[\w./-]+(?:\s+cli)?\s+to\s+")
# Two-word leads, normalised to one _*_VERBS entry each.
_PHRASE_LEADS = (
    (re.compile(r"^walk\s+(?:me|us)\s+through\b"), "walk me through"),
    (re.compile(r"^give\s+(?:me\s+|us\s+)?(?:a\s+|an\s+|the\s+)?(?:high-level\s+|brief\s+|short\s+|quick\s+)?"
                r"(?:overview|summary|explanation|rundown|breakdown|comparison|recommendation)\b"), "give overview"),
    (re.compile(r"^show\s+(?:me|us)\s+(?:how|why|what|where|which|when)\b"), "show me"),
    (re.compile(r"^tell\s+(?:me|us)\b"), "tell me"),
    (re.compile(r"^help\s+(?:me|us)\s+(?:understand|decide|choose|think)\b"), "help me"),
    (re.compile(r"^write\s+(?:up\s+)?(?:a\s+|an\s+|the\s+)?(?:short\s+|brief\s+)?(?:adr|rfc|design\s+(?:doc|note|document)"
                r"|proposal|(?:architecture\s+)?decision\s+record|threat\s+model|post-?mortem)\b"), "write adr"),
    (re.compile(r"^roll\s+back\b"), "roll back"),
    (re.compile(r"^clean\s+up\b"), "clean up"),
    (re.compile(r"^spin\s+up\b"), "spin up"),
    (re.compile(r"^tear\s+down\b"), "tear down"),
    (re.compile(r"^back\s+up\b"), "back up"),
)

# CLI tools. git counts only with an operational subcommand ("git rebase vs git
# merge" is a concept question); make only with a conventional target.
_OPS_COMMAND_RE = re.compile(
    r"(?<![\w-])(?:kubectl|docker|docker-compose|podman|terraform|helm|az|aws|gcloud|npm|npx|yarn|pnpm"
    r"|pip|pip3|pipx|uv|pytest|jest|vitest|mocha|tox|systemctl|journalctl|df|du|uname|lsof|netstat"
    r"|curl|wget|ssh|scp|rsync|crontab|bash|zsh|powershell|psql|minikube|kind|k9s|argocd|ansible"
    r"|kustomize|openssl|nslookup"
    r"|git\s+(?:push|pull|commit|status|log|fetch|clone|checkout|switch|tag|stash|reset|clean|gc|prune|bisect)"
    r"|make\s+(?:build|test|tests|install|all|clean|deploy|release|lint|check|up|down))(?![\w-])"
)
_INSTRUMENT_RE = re.compile(r"\b(?:with|using|via)\s+(?:the\s+)?(" + _OPS_COMMAND_RE.pattern + r")")
_SHELL_SCRIPT_RE = re.compile(r"\b(?:bash|shell|sh|zsh|powershell|cron)\s+(?:script|one-liner|job)s?\b")
_FILE_TARGET_RE = re.compile(
    r"(?<![\w-])[\w./-]*\w\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|css|scss|sass|less|html|vue|svelte|go|rs|java|kt"
    r"|rb|php|c|h|cc|cpp|hpp|cs|swift|sh|json|ya?ml|toml|ini|cfg|md|sql)(?![\w-])"
    r"|(?<![\w-])(?:readme|makefile|dockerfile)(?![\w-])"
)
# Identifiers in the original casing: parse_config, isExpired, paginate().
_IDENTIFIER_RE = re.compile(r"\b\w+\(\)|\b[a-z]+[A-Z]\w*\b|\b[a-z]+_[a-z_]+\b|\b[A-Z]\w*Error\b")
_TEST_WRITING_RE = re.compile(
    r"^(?:add|write|create|generate|cover|extend)\b.*?\b(?:unit\s+|integration\s+|regression\s+|e2e\s+|snapshot\s+)?"
    r"(?:tests?|test\s+cases?|specs?)\b"
)
# Multi-file scope. _SOFT_UNITS are "all X" that can live inside one file
# ("all references to the variable in auth.py"); a named file keeps those local.
_SOFT_UNITS = r"(?:imports?|callers?|call[\s-]sites?|usages?|references?|occurrences?)"
_MULTI_FILE_RES = (
    re.compile(r"\bacross\s+(?:the\s+)?(?:whole\s+|entire\s+)?(?:codebase|code\s*base|repo|repository|repositories"
               r"|project|workspace|monorepo|polyrepo|app|application|modules|files|services|packages|components"
               r"|every|all|each)\b"),
    re.compile(r"\b(?:every|all|each)\s+(?:of\s+the\s+|the\s+)?(?:\w+\s+)?(?:files?|modules?|services?|packages?|guards?"
               r"|repos?|repositories|components|directories|folders|microservices?|" + _SOFT_UNITS + r")\b"),
    re.compile(r"\b(?:everywhere|throughout\s+the\s+(?:codebase|repo|repository|project|app)"
               r"|(?:whole|entire)\s+(?:codebase|repo|repository|project|app)|(?:codebase|repo|project|workspace)[\s-]wide"
               r"|multi[\s-]file|cross[\s-]file|many\s+files|multiple\s+files|several\s+files)\b"),
    re.compile(r"\binto\s+(?:its\s+own|their\s+own|(?:a\s+)?(?:new|shared|common|separate)|separate|smaller|multiple"
               r"|several)\s+(?:\w+\s+)?(?:files?|modules?|packages?|librar(?:y|ies)|services?|crates?)\b"),
)
_SOFT_MULTI_RE = re.compile(r"\b(?:every|all|each)\s+(?:of\s+the\s+|the\s+)?(?:\w+\s+)?" + _SOFT_UNITS + r"\b")
# The IDE is chosen only when named as the *target*, not merely mentioned
# ("How does antigravity-ide receive deliveries?" is a question about it).
_IDE_TARGET_RE = re.compile(
    r"(?:^|\b(?:use|using|via|with|in|into|on|to|for|have|ask|let|assign|route|send|deliver|open|hand|pass)\s+"
    r"(?:it\s+|this\s+|that\s+|them\s+)?(?:(?:over\s+)?to\s+)?(?:the\s+)?)@?antigravity[\s-]+ide\b"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|;\s*")
_CLAUSE_SPLIT_RE = re.compile(r",\s*(?:and\s+|then\s+)?|\s+(?:and\s+then|and|then|but|so|&&)\s+")
_TIE_ORDER = ("antigravity", "cline", "kiro-cli")


def _vocab_re(terms) -> re.Pattern:
    """Whole-word matcher. Hyphens count as word characters, so 'ui' does not
    match 'brain-ui' and 'run' does not match '--dry-run'."""
    alternatives = sorted({re.escape(t).replace(r"\ ", r"[\s-]+") for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(?:" + "|".join(alternatives) + r")(?![\w-])")


_PROFILE_RES = {agent: _vocab_re(p["keywords"]) for agent, p in AGENT_PROFILES.items() if p["keywords"]}


def _lead(clause: str) -> str:
    """The clause's leading verb (or tool), with fillers and 'use X to' removed."""
    clause = _FILLER_RE.sub("", clause.strip(" \t\"'`*-:>([")).strip()
    clause = _USE_TOOL_RE.sub("", clause)
    for pattern, name in _PHRASE_LEADS:
        if pattern.match(clause):
            return name
    command = _OPS_COMMAND_RE.match(clause)
    if command:
        return "cmd:" + command.group(0)
    match = re.match(r"[a-z][\w'-]*", clause)
    return match.group(0) if match else ""


def _is_verb(lead: str, in_question: bool) -> bool:
    """Whether a clause lead is a word the router understands as an intent."""
    return (
        lead.startswith("cmd:") or lead in _STRONG_OPS_VERBS or lead in _WEAK_OPS_VERBS
        or lead in _CODE_VERBS or lead in _REASONING_VERBS or lead in _WH_WORDS
        or lead in _MULTI_FILE_VERBS or (lead in _AUX_WORDS and in_question)
    )


def _clauses(text: str) -> list[tuple[str, str, bool]]:
    """(clause, lead, in_question) for every clause, in order.

    Clauses inside a question sentence are not imperatives: in "What happens if
    we delete the pods and restart the node?" nothing asks for a restart.
    """
    out = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        polite = bool(re.match(r"(?:please\s+)?(?:can|could|would|will)\s+you\b", sentence))
        first = _lead(sentence)
        question = not polite and (
            first in _WH_WORDS or (first in _AUX_WORDS and sentence.endswith("?"))
            or (sentence.endswith("?") and first not in _STRONG_OPS_VERBS and not first.startswith("cmd:"))
        )
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            if clause.strip():
                out.append((clause, _lead(clause), question))
    return out


def _route_scores(task_text: str) -> tuple[dict[str, float], dict[str, list[str]], str]:
    """Additive evidence per agent, the reasons behind it, and the lead intent."""
    text = task_text.lower()
    scores = {"kiro-cli": 0.0, "cline": 0.0, "antigravity": 0.0}
    reasons: dict[str, list[str]] = {agent: [] for agent in scores}

    def add(agent: str, weight: float, reason: str) -> None:
        scores[agent] += weight
        reasons[agent].append(reason)

    keywords = {agent: sorted(set(m.group(0) for m in rx.finditer(text))) for agent, rx in _PROFILE_RES.items()}
    commands = sorted(set(re.sub(r"\s+", " ", m.group(0)) for m in _OPS_COMMAND_RE.finditer(text)))
    file_target = bool(_FILE_TARGET_RE.search(text))
    identifier = bool(_IDENTIFIER_RE.search(task_text))
    ops_context = bool(commands or keywords.get("kiro-cli"))
    code_evidence = bool(file_target or identifier or keywords.get("cline"))

    clauses = _clauses(text)
    # The lead clause is the first one that starts with a recognised verb, so a
    # context sentence ("The CI build keeps failing; rerun it") does not hide it.
    lead_index = next((i for i, (_, word, question) in enumerate(clauses) if _is_verb(word, question)), None)
    lead = clauses[lead_index][1] if lead_index is not None else ""
    lead_question = clauses[lead_index][2] if lead_index is not None else False

    # --- the lead clause: what the task asks for
    intent = ""
    ops_lead = False
    if lead.startswith("cmd:") or lead in _STRONG_OPS_VERBS:
        add("kiro-cli", _W_OPS_LEAD, f"leads with ops action '{lead.removeprefix('cmd:')}'")
        intent, ops_lead = "kiro-cli", True
    elif lead in _WEAK_OPS_VERBS and ops_context and not (lead in _CODE_VERBS and code_evidence):
        # "Build the Docker image" is ops; "Build a React component for the pod
        # list" names code, so the dual verb is left to the evidence below.
        add("kiro-cli", _W_OPS_ACTION, f"'{lead}' on ops targets")
        intent, ops_lead = "kiro-cli", True
    elif lead in _REASONING_VERBS or lead in _WH_WORDS or (lead in _AUX_WORDS and lead_question):
        intent = "antigravity"
    elif lead in _WEAK_OPS_VERBS and not code_evidence:
        intent = "kiro-cli"  # "Stop the service": no code in sight, so an ops verb
    elif lead in _CODE_VERBS or lead in _MULTI_FILE_VERBS:
        intent = "cline"
    elif lead in _WEAK_OPS_VERBS:
        intent = "kiro-cli"
    if not ops_lead and lead in _WEAK_OPS_VERBS and not (lead in _CODE_VERBS and code_evidence):
        add("kiro-cli", _W_WEAK_VERB, f"leads with '{lead}'")

    # --- later imperative clauses: "... and restart them", "? Run it", "and fix it"
    later_action = False
    code_verb = lead if lead in _CODE_VERBS else ""
    for clause, clause_lead, in_question in clauses[(lead_index or 0) + 1:]:
        if in_question:
            continue
        if not ops_lead and (clause_lead.startswith("cmd:") or clause_lead in _STRONG_OPS_VERBS
                             or (clause_lead in _WEAK_OPS_VERBS and ops_context and clause_lead not in _CODE_VERBS)):
            add("kiro-cli", _W_OPS_ACTION, f"then '{clause_lead.removeprefix('cmd:')}'")
            ops_lead = later_action = True
        elif clause_lead in _CODE_VERBS and code_evidence and not code_verb:
            code_verb, later_action = clause_lead, True

    # A CLI tool named as the instrument ("Describe the pod with kubectl",
    # "Check disk usage with df -h") means the task executes it.
    instrument = _INSTRUMENT_RE.search(text)
    if instrument and not ops_lead:
        add("kiro-cli", _W_OPS_ACTION, f"runs '{instrument.group(1)}'")
        ops_lead = later_action = True

    if intent == "antigravity":
        # A question followed by an action is context for that action.
        weight = _W_REASONING / 2 if later_action else _W_REASONING
        add("antigravity", weight, f"asks to '{lead}'" + (" (then acts)" if later_action else ""))

    # --- supporting evidence
    for command in commands:
        add("kiro-cli", _W_OPS_COMMAND, f"tool '{command}'")
    if _SHELL_SCRIPT_RE.search(text):
        add("kiro-cli", _W_SHELL_SCRIPT, "shell script")
    if code_verb:
        add("cline", _W_CODE_VERB if code_evidence else _W_WEAK_VERB, f"edit verb '{code_verb}'")
    if file_target:
        add("cline", _W_FILE_TARGET, "names a source file")
    if identifier:
        add("cline", _W_KEYWORD, "names a code identifier")
    if any(_TEST_WRITING_RE.match(_FILLER_RE.sub("", c.strip())) for c, cl, _ in clauses if cl in _CODE_VERBS):
        add("cline", _W_TEST_WRITING, "writes tests")
    if lead in ("design", "redesign") and len(keywords.get("cline", ())) >= 2 and keywords.get("antigravity") == ["design"]:
        add("cline", _W_VISUAL_DESIGN, "visual/styling design")
    for agent in ("kiro-cli", "cline", "antigravity"):
        if keywords.get(agent):
            add(agent, _W_KEYWORD * len(keywords[agent]), "terms: " + ", ".join(keywords[agent][:4]))

    # --- multi-file scope turns code work into antigravity work. "Delete all
    # files older than 7 days" is an ops action over data, not a code change, so
    # an ops lead without any code evidence keeps its scope out of this.
    multi = [rx.search(text).group(0) for rx in _MULTI_FILE_RES if rx.search(text)]
    if multi and file_target and all(_SOFT_MULTI_RE.fullmatch(m) for m in multi):
        multi = []  # "all references to the variable in auth.py" stays in one file
    if lead in _MULTI_FILE_VERBS:
        multi.append(lead)
    if multi and not (ops_lead and not code_evidence):
        add("antigravity", _W_MULTI_FILE, f"multi-file scope '{multi[0]}'")
        if intent == "cline":
            intent = "antigravity"
    return scores, reasons, intent


def classify_task(task_text: str) -> tuple[str, float, str]:
    """Route a task to (agent, confidence, rationale).

    Confidence reflects how clearly the winner beat the runner-up: a tie is 0.5,
    a decisive win approaches 0.95. antigravity-api is never returned (it is
    reached through balance_antigravity) and antigravity-ide only when named.
    """
    text = task_text.strip()
    if _IDE_TARGET_RE.search(text.lower()):
        return "antigravity-ide", 0.95, "Antigravity IDE requested by name (delivery only; a human drives it)"

    scores, reasons, intent = _route_scores(text)
    top = max(scores.values())
    if top == 0.0:
        # No signal at all. antigravity is the headless generalist; the IDE is
        # never a fallback because a task defaulted to it would never execute.
        return "antigravity", 0.5, "No routing signal; defaulted to headless Antigravity for reasoning"
    order = ((intent,) if intent else ()) + tuple(a for a in _TIE_ORDER if a != intent)
    best = max(order, key=lambda agent: (scores[agent], -order.index(agent)))
    runner_up = max((a for a in order if a != best), key=lambda agent: scores[agent])
    margin = scores[best] - scores[runner_up]
    confidence = min(0.95, 0.5 + 0.45 * margin / (scores[best] + 2.0))
    rationale = (
        f"Routed to {best}: " + "; ".join(reasons[best][:4])
        + f" (score {scores[best]:.1f} vs {runner_up} {scores[runner_up]:.1f})"
    )
    if margin == 0.0:
        rationale += f"; tie broken by the task's leading intent ({intent or 'default'})"
    return best, round(confidence, 2), rationale


# ==============================================================================
# 2. Task Queue State Machine
# ==============================================================================
def _task_prompt(title: str, description: str = "") -> str:
    """The prompt the worker sends for a task (see execute_task_worker)."""
    prompt = title.strip()
    if description.strip():
        prompt += f"\n\nContext & Instructions:\n{description.strip()}"
    return prompt


def _selection_request(prompt: str):
    """The planner's SelectionRequest for ``prompt``, or None if it cannot be built."""
    try:
        from . import orchestrator  # type: ignore[attr-defined]
    except ImportError:
        import orchestrator  # type: ignore[no-redef]
    try:
        return orchestrator.selection_request(prompt)
    except Exception:
        # Routing is an optimization; never fail to queue a task over it.
        return None


def create_task(title: str, description: str = "", preferred_agent: str | None = None, repo: str = "") -> dict:
    """Creates a new task in pending state."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    assigned_agent, confidence, rationale = (
        (preferred_agent, 1.0, "Manually specified")
        if preferred_agent
        else classify_task(f"{title} {description}")
    )
    if not preferred_agent:
        # Both Antigravity accounts are headless workers, so alternate between them
        # instead of letting one idle. The selection request is computed first,
        # from the same prompt the worker will select a model for, so work that
        # needs the strongest tier is never balanced onto the capped API account.
        assigned_agent, balance_note = balance_antigravity(
            assigned_agent, _selection_request(_task_prompt(title, description))
        )
        if balance_note:
            rationale = f"{rationale}; {balance_note}"

    task_data = {
        "id": task_id,
        "title": title.strip(),
        "description": description.strip(),
        "assigned_to": assigned_agent,
        "confidence": confidence,
        "rationale": rationale,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "output": None,
        "error": None,
        "duration_seconds": None,
        # Explicit sandbox target. Empty means the worker infers it from the
        # task text; see resolve_target_repo.
        "repo": repo.strip(),
    }

    task_file = PENDING_DIR / f"{task_id}.json"
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task_data, f, indent=2)

    return task_data


STALE_AFTER_SECONDS = int(os.environ.get("BRAIN_SWARM_STALE_AFTER_SECONDS", "900"))
# Frontier reasoning models in plan mode routinely take longer than two minutes,
# so the old hardcoded 120s ceiling turned a slow-but-successful run into a
# spurious escalation once capability-based routing started choosing them. A real
# successful Antigravity run was observed taking 1170s across account failover, so
# a 300s ceiling silently killed good work; the default is raised to 1200s to sit
# comfortably above observed successful runs. Override with
# BRAIN_SWARM_TASK_TIMEOUT_SECONDS.
TASK_TIMEOUT_SECONDS = int(os.environ.get("BRAIN_SWARM_TASK_TIMEOUT_SECONDS", "1200"))
# Delivery agents cannot be executed, so a desktop notice is the only way the
# user learns a task is waiting. It is informational: it carries no actions and
# can never record an approval. Set BRAIN_DELIVERY_NOTICE=0 to silence it.
DELIVERY_NOTICE_ENABLED = (
    os.environ.get("BRAIN_DELIVERY_NOTICE", os.environ.get("BRAIN_APPROVAL_PROMPT", "1")) != "0"
)
OPEN_TASK_CARD_ENABLED = os.environ.get("BRAIN_OPEN_TASK_CARD", "1") != "0"
# Opening the card cannot start an in-editor agent, so the prompt is also placed
# on the clipboard and is ready to paste into the Agent panel.
CLIPBOARD_PROMPT_ENABLED = os.environ.get("BRAIN_CLIPBOARD_PROMPT", "1") != "0"


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _task_projection(task: dict, *, now: datetime | None = None) -> dict:
    """Return a read-only task projection; old records are never silently rewritten."""
    projected = dict(task)
    now = now or datetime.now(timezone.utc)
    status = projected.get("status", "unknown")
    evidence_time = _parse_timestamp(projected.get("heartbeat_at")) or _parse_timestamp(projected.get("updated_at")) or _parse_timestamp(projected.get("started_at")) or _parse_timestamp(projected.get("created_at"))
    if status == "in-progress" and evidence_time:
        age = max(0, int((now - evidence_time).total_seconds()))
        projected["age_seconds"] = age
        if age > STALE_AFTER_SECONDS and not projected.get("output") and not projected.get("error"):
            projected["display_status"] = "stale"
            projected["stale_reason"] = f"No heartbeat, output, error, or completion evidence for {age // 60} minutes"
            return projected
    projected["display_status"] = status
    return projected


def get_all_tasks() -> dict[str, list[dict]]:
    """Read task files into evidence-qualified state buckets without mutating them."""
    tasks = {"pending": [], "in-progress": [], "stale": [], "completed": [], "escalated": []}
    for state, folder in [("pending", PENDING_DIR), ("in-progress", IN_PROGRESS_DIR), ("completed", COMPLETED_DIR), ("escalated", ESCALATED_DIR)]:
        for file_path in folder.glob("*.json"):
            try:
                with open(file_path, "r", encoding="utf-8") as handle:
                    projected = _task_projection(json.load(handle))
                bucket = "stale" if projected.get("display_status") == "stale" else state
                tasks[bucket].append(projected)
            except Exception:
                continue
    for items in tasks.values():
        items.sort(key=lambda item: item.get("completed_at") or item.get("updated_at") or item.get("started_at") or item.get("created_at") or "", reverse=True)
    return tasks


def _delivery_dirs(agent: str) -> tuple[Path, Path, Path]:
    """Resolve an agent's delivery directories from module globals at call time.

    Reading globals here (instead of a static map) keeps every path patchable by
    tests via ``patch.multiple(swarm, CLINE_DELIVERIES_DIR=...)``.
    """
    if agent not in DELIVERY_AGENTS:
        raise ValueError(f"{agent} is not a delivery-based agent")
    prefix = agent.replace("-", "_").upper()
    return (
        globals()[f"{prefix}_DIR"],
        globals()[f"{prefix}_DELIVERIES_DIR"],
        globals()[f"{prefix}_TASK_FILE"],
    )


def _delivery_state(tasks: dict[str, list[dict]], agent: str) -> dict:
    """Return only lifecycle states evidenced by durable per-task delivery records."""
    installed, presence_basis = delivery_agent_installed(agent)
    state = {
        "availability": "ready" if installed else "unavailable",
        "basis": (
            f"{presence_basis}; no task delivered yet"
            if installed
            else f"{presence_basis}; no {agent} delivery or acknowledgement evidence"
        ),
        "installed": installed,
        "installation_basis": presence_basis,
        "task_id": None,
        "observed_at": None,
        "lifecycle": "unavailable",
        "agent": agent,
        "runtime": "in-editor chat session; no headless execution is claimed",
    }
    _, deliveries_dir, _ = _delivery_dirs(agent)
    deliveries: list[dict] = []
    for path in deliveries_dir.glob("*.json"):
        try:
            deliveries.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    if not deliveries:
        return state
    latest = max(deliveries, key=lambda item: item.get("updated_at") or item.get("staged_at") or "")
    lifecycle = latest.get("status", "unknown")
    recommendation = latest.get("model_recommendation") or {}
    state.update({
        "task_id": latest.get("task_id"),
        "observed_at": latest.get("updated_at") or latest.get("staged_at"),
        "lifecycle": lifecycle,
        "model_recommended_tier": recommendation.get("tier"),
        "model_candidates": recommendation.get("candidates") or [],
        "model_reported": latest.get("model_reported"),
    })
    descriptions = {
        f"staged_for_{agent}": ("staged", f"task delivered to {agent}; acknowledgement not yet recorded"),
        f"acknowledged_by_{agent}": ("acknowledged", f"{agent} acknowledgement recorded; work has not yet reported progress"),
        "working": ("working", f"{agent} progress evidence recorded"),
        "awaiting_verification": ("awaiting_verification", f"{agent} submitted work awaiting verification"),
        "completed": ("idle", f"latest {agent} task completed with recorded lifecycle evidence"),
        "escalated": ("blocked", f"latest {agent} task escalated"),
    }
    state["availability"], state["basis"] = descriptions.get(lifecycle, ("unknown", f"unrecognized {agent} delivery lifecycle"))
    return state


def _cline_delivery_state(tasks: dict[str, list[dict]]) -> dict:
    """Backward-compatible alias for the Cline delivery lifecycle state."""
    return _delivery_state(tasks, "cline")


def _antigravity_ide_delivery_state(tasks: dict[str, list[dict]]) -> dict:
    """Evidence-qualified Antigravity IDE delivery lifecycle state."""
    return _delivery_state(tasks, "antigravity-ide")


def _antigravity_api_state() -> dict:
    """Availability of the second Antigravity account, for the status UI.

    Two independent facts: the executable exists, and a key is attached. Reporting
    them separately keeps "installed" from being mistaken for "usable".
    """
    account = AgentAccount("api-key", "antigravity-api", "api", ANTIGRAVITY_API_DATA_DIR, ANTIGRAVITY_API_MODELS)
    attached, basis = account_available(account)
    if not ANTIGRAVITY_BIN.exists():
        return {"availability": "unavailable", "basis": "antigravity executable not found", "key_attached": False, "models": list(ANTIGRAVITY_API_MODELS)}
    return {
        "availability": "available" if attached else "unattached",
        "basis": basis,
        "key_attached": attached,
        "auth": "gemini-api-key",
        "data_dir": account.data_dir,
        "models": list(ANTIGRAVITY_API_MODELS),
        "scope": "gemini models only; billed to the API key",
    }


def token_account(tasks: dict[str, list[dict]] | None = None) -> dict:
    """Aggregate real token usage and cost across every recorded task.

    Only the direct-provider worker reports usage, because a provider returns it
    in the response. Agent CLIs bill inside their own account and expose no
    per-call usage, so they are counted separately as "opaque" rather than
    guessed at — an invented number would make this view fiction.
    """
    tasks = tasks if tasks is not None else get_all_tasks()
    by_provider: dict[str, dict] = {}
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "cost_usd": 0.0, "metered_tasks": 0, "free_tasks": 0}
    opaque = {"tasks": 0, "agents": {}}

    for bucket in tasks.values():
        for t in bucket:
            usage = t.get("token_usage")
            if not usage:
                # A CLI-executed task: real work, but no usage is exposed.
                if t.get("status") in ("completed", "escalated") and t.get("assigned_to"):
                    agent = t["assigned_to"]
                    # Delivery agents never execute, and an API task that
                    # recorded no usage failed before it called a provider, so
                    # neither belongs in the "CLI bills privately" bucket.
                    if agent not in DELIVERY_AGENTS and agent != API_AGENT:
                        opaque["tasks"] += 1
                        opaque["agents"][agent] = opaque["agents"].get(agent, 0) + 1
                continue
            provider = usage.get("provider") or "unknown"
            row = by_provider.setdefault(provider, {
                "provider": provider, "tasks": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
                "models": {}, "billing": usage.get("billing", "unknown"),
            })
            row["tasks"] += 1
            for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = int(usage.get(field_name) or 0)
                row[field_name] += value
                totals[field_name] += value
            cost = float(usage.get("cost_usd") or 0.0)
            row["cost_usd"] = round(row["cost_usd"] + cost, 6)
            totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)
            model = usage.get("model") or "unknown"
            row["models"][model] = row["models"].get(model, 0) + 1
            if usage.get("billing") == "free tier" or cost == 0.0:
                totals["free_tasks"] += 1
            else:
                totals["metered_tasks"] += 1

    return {
        "totals": totals,
        "by_provider": sorted(by_provider.values(), key=lambda r: -r["total_tokens"]),
        "opaque": opaque,
        "note": ("cost_usd covers provider-API tasks only; agent CLIs bill inside "
                 "their own accounts and report no per-call usage"),
    }


def get_swarm_snapshot(history_limit: int = 12) -> dict:
    tasks = get_all_tasks()
    completed = tasks["completed"][:max(1, min(history_limit, 100))]
    return {
        "tasks": {"pending": tasks["pending"], "in-progress": tasks["in-progress"], "stale": tasks["stale"], "completed": completed, "escalated": tasks["escalated"]},
        "token_account": token_account(tasks),
        "stats": {"total": sum(len(items) for items in tasks.values()), "pending": len(tasks["pending"]), "in_progress": len(tasks["in-progress"]), "stale": len(tasks["stale"]), "completed": len(tasks["completed"]), "completed_total": len(tasks["completed"]), "escalated": len(tasks["escalated"])},
        "agents": {
            "antigravity": {"availability": "available" if ANTIGRAVITY_BIN.exists() else "unavailable", "basis": "local executable discovery only"},
            "antigravity-api": _antigravity_api_state(),
            "kiro-cli": {"availability": "available" if KIRO_CLI_BIN.exists() else "unavailable", "basis": "local executable discovery only"},
            "cline": {"availability": "available" if CLINE_BIN.exists() else "unavailable", "basis": "local executable discovery only"},
            "antigravity-ide": _delivery_state(tasks, "antigravity-ide"),
        },
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


# ==============================================================================
# 3. Durable In-Editor Delivery Lifecycle (Cline, Antigravity IDE)
# ==============================================================================
# Delivery records are the only source of truth for in-editor agents. Writing a
# delivery is not evidence of execution; CURRENT_TASK.md is a human-readable
# pointer only.
def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _exact_task(task_id: str) -> tuple[dict, Path, str]:
    clean = task_id.strip()
    if not re.fullmatch(r"task-[a-f0-9]{8}", clean):
        raise ValueError("task_id must be an exact task-xxxxxxxx identifier")
    for state, folder in (("pending", PENDING_DIR), ("in-progress", IN_PROGRESS_DIR), ("completed", COMPLETED_DIR), ("escalated", ESCALATED_DIR)):
        path = folder / f"{clean}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), path, state
    raise FileNotFoundError(f"task {clean} was not found")


def _delivery_path(task_id: str, agent: str = "cline") -> Path:
    _, deliveries_dir, _ = _delivery_dirs(agent)
    return deliveries_dir / f"{task_id}.json"


def _append_event(delivery: dict, event: str, note: str = "", session: str = "") -> None:
    timestamp = datetime.now(timezone.utc).isoformat()
    delivery.setdefault("events", []).append({"event": event, "at": timestamp, "note": note[:2000], "session": session[:200]})
    delivery["events"] = delivery["events"][-50:]
    delivery["updated_at"] = timestamp


def _recommendation_for(prompt: str, agent: str) -> dict | None:
    """Compute a model recommendation for an in-editor delivery agent.

    Returns a plain dict for embedding in the delivery record, or ``None`` if the
    recommendation could not be derived. This never changes the agent's model;
    in-editor agents pick their own, and only they can report what they used.
    """
    try:
        from . import orchestrator  # type: ignore[attr-defined]
        from .model_policy import ContextSize, SelectionRequest, recommend_for_delivery_agent
    except ImportError:
        import orchestrator  # type: ignore[no-redef]
        from model_policy import ContextSize, SelectionRequest, recommend_for_delivery_agent
    try:
        planned = orchestrator.plan_tasks([prompt]).tasks[0]
        context = ContextSize.LARGE if len(prompt) > 4000 else (ContextSize.MEDIUM if len(prompt) > 800 else ContextSize.SMALL)
        return recommend_for_delivery_agent(
            agent,
            SelectionRequest(
                action=planned.action,
                risk=planned.risk,
                complexity=planned.complexity,
                context=context,
            ),
        ).as_dict()
    except Exception:
        return None


def _notify_delivery_staged(agent: str, task: dict, recommendation: dict | None) -> bool:
    """Tell the desktop that a task is waiting. This never records approval.

    An earlier version showed an actionable notification whose Approve action
    recorded an acknowledgement. That fabricated evidence: `notify-send --wait`
    reports the *first* action, not the clicked one, on notification daemons that
    return an action when the notification expires. quickshell, the daemon on this
    machine, prints `approve` after roughly three seconds with nobody touching it,
    so every delivery was auto-acknowledged as "approved by user at the desktop
    prompt" before a human ever saw it.

    The notice is therefore informational only. Approval happens where it can be
    attributed: the dashboard row or `brain swarm <agent> acknowledge`.
    Returns whether a notice was actually sent.
    """
    if not DELIVERY_NOTICE_ENABLED:
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    notify = shutil.which("notify-send")
    if not notify:
        return False

    title = (task.get("title") or "")[:140]
    tier = (recommendation or {}).get("tier") or "unknown"
    candidates = (recommendation or {}).get("candidates") or []
    detail = f"Recommended: {tier} tier" + (f"\nTop model: {candidates[0]}" if candidates else "")
    body = f"{title}\n\n{detail}\n\nApprove it on the dashboard; the briefing is on your clipboard."
    try:
        # No actions and no --wait: nothing here may be mistaken for a human
        # decision, and the swarm is never blocked on the desktop.
        subprocess.Popen(
            [notify, "-u", "normal", "-a", "Shared Brain", f"Swarm task waiting for {agent}", body],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def _copy_to_clipboard(text: str) -> bool:
    """Put the paste-ready prompt on the clipboard for an in-editor agent.

    antigravity-ide is a VS Code style launcher: `antigravity-ide -r <file>` opens
    a file and nothing more. It has no flag that can send a prompt to its Agent
    panel, so opening the card cannot start the work. The clipboard is the one
    channel that removes the retyping step, which is why delivery puts the prompt
    there. Set BRAIN_CLIPBOARD_PROMPT=0 to disable.
    """
    if not CLIPBOARD_PROMPT_ENABLED:
        return False
    if not text.strip():
        return False
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = [shutil.which("wl-copy") or "wl-copy"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        command = [shutil.which("xclip") or "xclip", "-selection", "clipboard"]
    else:
        return False
    try:
        # wl-copy keeps a background process alive to own the selection, so it is
        # started detached and never waited on.
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    try:
        assert process.stdin is not None
        process.stdin.write(text.encode("utf-8"))
        process.stdin.close()
    except OSError:
        return False
    return True


def _open_task_card(agent: str, card: Path) -> bool:
    """Open the delivery card in the agent's own editor.

    Desktop notification actions are not rendered by every shell, so the card is
    also opened directly in the editor. That is the one surface an in-editor agent
    is guaranteed to see. Set BRAIN_OPEN_TASK_CARD=0 to disable.
    """
    if not OPEN_TASK_CARD_ENABLED:
        return False
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    launcher = shutil.which("antigravity-ide") if agent == "antigravity-ide" else None
    if not launcher:
        return False
    try:
        subprocess.Popen(
            [launcher, "-r", str(card)],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def stage_task_for_delivery(task: dict, agent: str) -> tuple[str, str]:
    """Durably deliver a task to an in-editor agent.

    Writing a delivery record is *not* evidence that the agent is running; the
    lifecycle only advances when the agent itself records an acknowledgement.
    """
    if agent not in DELIVERY_AGENTS:
        return "", f"{agent} is not a delivery-based agent"
    _, _, task_file = _delivery_dirs(agent)
    task_id = task["id"]
    staged_event = f"staged_for_{agent}"
    timestamp = datetime.now(timezone.utc).isoformat()
    title = task.get("title", "")
    description = task.get("description") or title
    recommendation = _recommendation_for(f"{title}\n{task.get('description','')}".strip(), agent)
    delivery = {
        "version": 1,
        "agent": agent,
        "task_id": task_id,
        "title": title,
        "status": staged_event,
        "staged_at": timestamp,
        "updated_at": timestamp,
        "model_recommendation": recommendation,
        "model_reported": None,
        "events": [{"event": staged_event, "at": timestamp, "note": "task delivery created", "session": ""}],
    }
    _write_json_atomic(_delivery_path(task_id, agent), delivery)
    try:
        stored, source, state = _exact_task(task_id)
        stored_updates = {"stage_state": staged_event, "updated_at": timestamp, "assigned_to": agent}
        if recommendation:
            stored_updates["model_recommendation"] = recommendation
        if state == "pending":
            stored_updates.update({"status": "in-progress", "staged_at": timestamp})
            stored.update(stored_updates)
            destination = IN_PROGRESS_DIR / source.name
            _write_json_atomic(destination, stored)
            source.unlink()
        else:
            stored.update(stored_updates)
            _write_json_atomic(source, stored)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        return "", f"unable to persist {agent} task state: {error}"

    if recommendation:
        candidates = recommendation.get("candidates") or []
        model_block = (
            f"\n## Recommended model\n"
            f"- **Capability tier**: `{recommendation['tier']}`\n"
            f"- **Work type**: `{recommendation['specialization']}`\n"
            f"- **Large context needed**: {recommendation['needs_large_context']}\n"
            f"- **Why**: {recommendation['rationale']}\n"
            + (
                "- **Candidates, best first**:\n"
                + "".join(f"  - `{c}`\n" for c in candidates)
                if candidates
                else "- **Candidates**: none discoverable locally; pick the closest match to the tier in your own model picker\n"
            )
            + f"\nSwitch your model before starting, then record what you actually used:\n"
            f"```bash\nbrain swarm {agent} acknowledge {task_id} --model <the-model-you-switched-to>\n```\n"
        )
    else:
        model_block = ""

    top_candidate = ((recommendation or {}).get("candidates") or [None])[0]
    # This is what the human pastes into the Agent panel. The delivery pointer is
    # a file, and opening a file does not brief an agent, so the briefing is
    # written out in full and put on the clipboard.
    paste_prompt = (
        f"You are executing Shared Brain swarm task {task_id} in this workspace.\n\n"
        f"Task: {title}\n\n"
        f"Instructions:\n{description}\n\n"
        f"When you have finished, report evidence from a terminal:\n"
        f"  brain swarm {agent} progress {task_id} -n \"<what you changed>\"\n"
        f"  brain swarm {agent} complete {task_id} -n \"<what you verified>\"\n"
    )
    start_block = (
        f"## How to start this task\n"
        f"Opening this file does not brief the Agent panel; `{agent}` has no flag that can\n"
        f"send it a prompt. Do these three things:\n\n"
        + (f"1. Switch the model picker to `{top_candidate}`.\n" if top_candidate else "1. Switch the model picker to the recommended tier below.\n")
        + f"2. Paste the briefing into the Agent panel. It is already on your clipboard;\n"
        f"   otherwise copy the block below.\n"
        f"3. Record the acknowledgement so the swarm stops counting this as unstarted:\n"
        f"   `brain swarm {agent} acknowledge {task_id} --model <the-model-you-switched-to>`\n\n"
        f"```text\n{paste_prompt}```\n"
    )
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        # The pointer lives inside the brain store, so every note-level tool reads
        # it — including `brain validate`, which hard-failed on this file after
        # every delivery because it had no frontmatter. Emit a minimal valid
        # header. `type: note` is deliberate: this is a pointer, not a handoff, so
        # it must not be validated against the strict handoff schema.
        f"---\n"
        f"title: CURRENT_TASK\n"
        f"type: note\n"
        f"tags: [swarm, delivery, {agent}]\n"
        f"---\n\n"
        f"# {agent} delivery pointer\n\n"
        f"- **Task ID**: `{task_id}`\n"
        f"- **Title**: {title}\n"
        f"- **Delivery record**: `{_delivery_path(task_id, agent)}`\n"
        f"- **Lifecycle**: `{staged_event}`\n\n"
        f"## Instructions\n{description}\n\n"
        f"{start_block}"
        f"{model_block}\n"
        f"{agent} must acknowledge with: `brain swarm {agent} acknowledge {task_id}`\n",
        encoding="utf-8",
    )
    notified = _notify_delivery_staged(agent, task, recommendation)
    opened = _open_task_card(agent, task_file)
    copied = _copy_to_clipboard(paste_prompt) if opened else False
    suffix = ""
    if recommendation:
        top = (recommendation.get("candidates") or [None])[0]
        suffix = f" Recommended model tier {recommendation['tier']}" + (f", top candidate {top}." if top else ".")
    if notified:
        suffix += " Desktop notice shown; it records nothing."
    if opened:
        suffix += " Task card opened in the editor."
    if copied:
        suffix += " Briefing copied to the clipboard; paste it into the Agent panel."
    return f"{agent} delivery recorded for {task_id}; awaiting acknowledgement.{suffix}", ""


def stage_task_for_cline(task: dict) -> tuple[str, str]:
    """Durably deliver a task to Cline; this is not evidence that it is running."""
    return stage_task_for_delivery(task, "cline")


def stage_task_for_antigravity_ide(task: dict) -> tuple[str, str]:
    """Durably deliver a task to Antigravity IDE; delivery is not execution."""
    return stage_task_for_delivery(task, "antigravity-ide")


def _update_delivery(agent: str, task_id: str, event: str, *, note: str = "", session: str = "", model: str = "") -> dict:
    if agent not in DELIVERY_AGENTS:
        raise ValueError(f"{agent} is not a delivery-based agent")
    staged_event = f"staged_for_{agent}"
    ack_event = f"acknowledged_by_{agent}"
    delivery_file = _delivery_path(task_id, agent)
    if not delivery_file.exists():
        raise FileNotFoundError(f"no {agent} delivery exists for {task_id}")
    delivery = json.loads(delivery_file.read_text(encoding="utf-8"))
    allowed = {
        ack_event: {staged_event, ack_event},
        "working": {ack_event, "working"},
        "awaiting_verification": {ack_event, "working", "awaiting_verification"},
        "completed": {ack_event, "working", "awaiting_verification"},
        "escalated": {staged_event, ack_event, "working", "awaiting_verification"},
    }
    if delivery.get("status") not in allowed[event]:
        raise ValueError(f"cannot transition {agent} task from {delivery.get('status')} to {event}")
    task, source, state = _exact_task(task_id)
    timestamp = datetime.now(timezone.utc).isoformat()
    delivery["status"] = event
    # The agent reports the model it actually switched to. This is the only
    # trustworthy source for an in-editor agent's model, so it is recorded
    # verbatim and never inferred from the recommendation.
    if model.strip():
        delivery["model_reported"] = model.strip()
        task["model_reported"] = model.strip()
    _append_event(delivery, event, note, session)
    task.update({"stage_state": event, "updated_at": timestamp, "heartbeat_at": timestamp, f"{agent}_session": session or task.get(f"{agent}_session")})
    if event in {ack_event, "working", "awaiting_verification"}:
        task["status"] = "in-progress"
        task[f"{agent}_progress"] = note
        destination = IN_PROGRESS_DIR / source.name
    elif event == "completed":
        task.update({"status": "completed", "completed_at": timestamp, "output": note or task.get("output") or f"{agent} completed task; review recorded delivery evidence."})
        destination = COMPLETED_DIR / source.name
    else:
        task.update({"status": "escalated", "error": note or f"{agent} escalated task"})
        destination = ESCALATED_DIR / source.name
    _write_json_atomic(destination, task)
    if destination != source:
        source.unlink(missing_ok=True)
    _write_json_atomic(delivery_file, delivery)
    return {"task": task, "delivery": delivery}


def _update_cline(task_id: str, event: str, *, note: str = "", session: str = "") -> dict:
    return _update_delivery("cline", task_id, event, note=note, session=session)


def delivery_acknowledge(agent: str, task_id: str, session: str = "", note: str = "", model: str = "") -> dict:
    return _update_delivery(agent, task_id, f"acknowledged_by_{agent}", note=note or f"{agent} acknowledged delivery", session=session, model=model)


def delivery_progress(agent: str, task_id: str, note: str, session: str = "", model: str = "") -> dict:
    if not note.strip():
        raise ValueError("progress note is required")
    return _update_delivery(agent, task_id, "working", note=note, session=session, model=model)


def delivery_awaiting_verification(agent: str, task_id: str, note: str, session: str = "", model: str = "") -> dict:
    return _update_delivery(agent, task_id, "awaiting_verification", note=note, session=session, model=model)


def delivery_complete(agent: str, task_id: str, note: str = "", session: str = "", model: str = "") -> dict:
    return _update_delivery(agent, task_id, "completed", note=note, session=session, model=model)


def delivery_escalate(agent: str, task_id: str, note: str, session: str = "", model: str = "") -> dict:
    if not note.strip():
        raise ValueError("escalation note is required")
    return _update_delivery(agent, task_id, "escalated", note=note, session=session, model=model)


def cline_acknowledge(task_id: str, session: str = "", note: str = "") -> dict:
    return delivery_acknowledge("cline", task_id, session=session, note=note)


def cline_progress(task_id: str, note: str, session: str = "") -> dict:
    return delivery_progress("cline", task_id, note, session=session)


def cline_awaiting_verification(task_id: str, note: str, session: str = "") -> dict:
    return delivery_awaiting_verification("cline", task_id, note, session=session)


def cline_complete(task_id: str, note: str = "", session: str = "") -> dict:
    return delivery_complete("cline", task_id, note=note, session=session)


def cline_escalate(task_id: str, note: str, session: str = "") -> dict:
    return delivery_escalate("cline", task_id, note, session=session)


# ==============================================================================
# 4. Headless Parallel Execution Dispatcher
# ==============================================================================
def _selection_for(prompt: str, agent: str):
    """Derive a per-agent model selection from the task text.

    Uses ``orchestrator.selection_request``, the exact inputs ``brain plan`` uses,
    so a plan's model is the model the worker sends for the same agent and task.
    It no longer goes through ``plan_tasks``: that also routes the task, which is
    wasted work here because the agent is already assigned. Returns ``None`` when
    the agent has no model flag, so callers fall back to the agent's own default.
    """
    try:
        from .model_policy import SELECTABLE_AGENTS, AgentTarget, select_for_agent  # type: ignore[attr-defined]
    except ImportError:
        from model_policy import SELECTABLE_AGENTS, AgentTarget, select_for_agent  # type: ignore[no-redef]
    try:
        target = AgentTarget(agent)
    except ValueError:
        return None
    if target not in SELECTABLE_AGENTS:
        return None
    request = _selection_request(prompt)
    if request is None:
        return None
    try:
        return select_for_agent(target, request)
    except Exception:
        # Routing is an optimization; never fail a task because it could not be planned.
        return None


_CREDITS_RE = re.compile(r"Credits:\s*([0-9]+(?:\.[0-9]+)?)")


def _captured_credits(text: str) -> float | None:
    """Extract actual credits consumed from Kiro CLI output when it reports them."""
    matches = _CREDITS_RE.findall(text or "")
    if not matches:
        return None
    try:
        return round(sum(float(value) for value in matches), 4)
    except ValueError:
        return None


# ------------------------------------------------------------------------------
# Approval gate
# ------------------------------------------------------------------------------
# model_policy flags a high-risk mutation with ``requires_approval``. Headless
# agents run with their own approval prompts disabled (--trust-all-tools,
# --auto-approve, --dangerously-skip-permissions), so this gate is the only
# thing standing between "Apply the terraform plan in PROD" and it happening.
#
# A held task is parked in escalated/ with stage_state ``awaiting_approval``,
# the same status/stage_state convention dashboard_execution.py uses, so the
# dashboard's "needs intervention" column and ``brain swarm status`` both show it
# without a new queue folder. Only ``brain swarm approve``, typed at a terminal,
# records an approval (see decisions/desktop-notifications-must-never-record-
# approval), and nothing in the execution path grants one.
#
# Known limitation, stated plainly: this is a same-user store. Any process
# running as the user can write a task JSON with an approval block into
# pending/, and could fake a terminal (os.openpty) with no agent CLI among
# its ancestors. The gate stops the swarm from running high-risk work on its
# own, and stops an agent from casually approving its own task. It cannot
# prove a human against a same-user process that sets out to forge one. That
# needs a boundary outside this user account, such as a separate OS user or a
# hardware-backed confirmation.
AWAITING_APPROVAL = "awaiting_approval"
# Delivery agents are driven by a human in an editor, and the provider-API
# worker is pure inference with no tools, so neither can mutate anything itself.
_APPROVAL_EXEMPT_AGENTS = (*DELIVERY_AGENTS, API_AGENT)

# Defence in depth for the approval gate, independent of the planner's word
# list. The planner only knows exact verbs ("delete", "restart"), so
# "Deleting the prod namespace", "git push --force" or "rm -rf /srv/data" read
# as low risk. These patterns hold a task for approval whatever the planner
# says. The verb rules require the verb to open a clause, so "Explain how we
# deploy to production" is not held, while "Restarting the payments service in
# prod" is. Literal destructive commands are held wherever they appear.
_MUTATING_VERB = (
    r"(?:delet(?:e|es|ed|ing)|remov(?:e|es|ing)|drop(?:s|ping)?|destroy(?:s|ing)?|restart(?:s|ing)?|"
    r"reboot(?:s|ing)?|apply(?:ing)?|deploy(?:s|ing)?|roll(?:ing)?[ -]?back|scal(?:e|es|ing)|"
    r"migrat(?:e|es|ing)|truncat(?:e|es|ing)|wip(?:e|es|ing)|purg(?:e|es|ing)|kill(?:s|ing)?|"
    r"shut(?:s|ting)?\s*down|terminat(?:e|es|ing)|stop(?:s|ping)?|upgrad(?:e|es|ing)|revert(?:s|ing)?|"
    r"reset(?:s|ting)?|overwrit(?:e|es|ing)|force[- ]?push(?:es|ing)?)"
)
_CLAUSE_START = r"(?:^|[\n.;:!?]|\b(?:then|and|also|please|now|next)\b)\s*(?:(?:please|now|then|go ahead and|run)\s+)*"
_DESTRUCTIVE_RE = re.compile(
    r"(\brm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)[a-z]*\b"
    r"|\bgit\s+push\b[^\n;]*\s(?:--force(?:-with-lease)?|-f)\b"
    r"|\bforce[- ]?push"
    r"|\b(?:drop|truncate)\s+(?:the\s+)?(?:[\w.`\"-]+\s+)?(?:table|database|schema|keyspace|collection)s?\b"
    r"|\bterraform\s+destroy\b|\bkubectl\s+delete\b|\baz\s+group\s+delete\b"
    + r"|" + _CLAUSE_START + r"(?:destroy(?:s|ing)?|tear(?:s|ing)?\s+down|wip(?:e|es|ing)|purg(?:e|es|ing)|nuk(?:e|es|ing))\b"
    r"[^\n.;]{0,60}\b(?:cluster|namespace|database|db|environment|env|resource\s+group|stack|infrastructure|"
    r"infra|volume|bucket|server|vm|deployment)s?\b"
    + r"|" + _CLAUSE_START + _MUTATING_VERB + r"\b[^\n.;]{0,80}\b(?:prod|production)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def _assess_approval(prompt: str):
    """Model-policy verdict for a task, resolved against kiro-cli.

    Unlike _selection_for, which swallows every planner error because model
    choice is only an optimisation, this lets errors propagate so the approval
    gate can fail closed. requires_approval depends only on the task's action
    and risk, so resolving it against one fixed headless agent is sufficient.
    """
    try:
        from . import orchestrator  # type: ignore[attr-defined]
        from .model_policy import AgentTarget, ContextSize, SelectionRequest, select_for_agent
    except ImportError:
        import orchestrator  # type: ignore[no-redef]
        from model_policy import AgentTarget, ContextSize, SelectionRequest, select_for_agent
    planned = orchestrator.plan_tasks([prompt]).tasks[0]
    return select_for_agent(
        AgentTarget("kiro-cli"),
        SelectionRequest(
            action=planned.action,
            risk=planned.risk,
            complexity=planned.complexity,
            context=ContextSize.SMALL,
        ),
    )


def _approval_reason(prompt: str, agent: str, selection) -> str:
    """Why this task needs a human approval to run, or "" when it does not.

    The gate fails closed: if the risk cannot be assessed at all, the task is
    held rather than run.
    """
    if agent in _APPROVAL_EXEMPT_AGENTS:
        return ""
    destructive = _DESTRUCTIVE_RE.search(prompt or "")
    if destructive:
        return (
            f"destructive operation detected ({destructive.group(0).strip()[:80]!r}); "
            f"high-risk mutation requires approval before execution"
        )
    if selection is not None and getattr(selection, "requires_approval", False):
        return getattr(selection, "rationale", "") or "high-risk mutation requires approval before execution"
    try:
        assessed = _assess_approval(prompt)
    except Exception as error:  # noqa: BLE001 - any failure must hold, not run
        return (
            f"the task's risk could not be assessed ({type(error).__name__}: {error}); "
            f"held for approval because the gate fails closed"
        )
    if getattr(assessed, "requires_approval", False):
        return getattr(assessed, "rationale", "") or "high-risk mutation requires approval before execution"
    return ""


def _approval_required(prompt: str, agent: str, selection) -> bool:
    """Whether this task needs a human approval to run (see _approval_reason)."""
    return bool(_approval_reason(prompt, agent, selection))


# Agent CLIs whose presence among the approving process's ancestors means an
# agent, not a person, is at the keyboard. Matched against each ancestor's
# comm and the basenames of argv[0] and argv[1] (cline runs as `node .../cline`).
# IDE processes (antigravity-ide, code, kiro) and terminal wrappers are
# deliberately absent: a human types in their integrated terminals.
_AGENT_PROCESS_NAMES = frozenset({
    "kiro-cli", "kiro-cli-chat", "cline", "agy", "antigravity", "gemini",
    "openhands", "claude", "q", "qchat", "codex", "aider",
})


def _agent_ancestor() -> str:
    """Name of the first agent CLI among this process's ancestors, or "".

    Linux /proc only; elsewhere the check is skipped and the terminal check
    alone applies. A deterrent against an agent approving its own work, not a
    proof of a human (see the limitation above).
    """
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            names = {(proc / "comm").read_text(encoding="utf-8", errors="replace").strip()}
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            names.update(os.path.basename(a.decode("utf-8", "replace")) for a in argv[:2] if a)
            stat = (proc / "stat").read_text(encoding="utf-8", errors="replace")
            parent = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return ""
        hit = names & _AGENT_PROCESS_NAMES
        if hit:
            return sorted(hit)[0]
        pid = parent
    return ""


def _approval_key_path() -> Path:
    """Per-user secret that approvals are signed with, kept OUTSIDE the brain store.

    Every agent reads and writes the store, so a fingerprint computable from
    the store alone (the task text) could be forged by writing an "approved"
    record into pending/. The key lives in the user's state directory instead,
    created 0600 the first time a human approves something.
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "agentic-brain" / "approval.key"


def _approval_key(create: bool = False) -> bytes | None:
    path = _approval_key_path()
    try:
        key = path.read_bytes()
        return key or None
    except FileNotFoundError:
        if not create:
            return None
    except OSError:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another approve created it first
        return path.read_bytes() or None
    key = os.urandom(32)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def _approval_fingerprint(task: dict, create_key: bool = False) -> str:
    """Bind an approval to the exact task text that was approved.

    Editing the title or description after approval invalidates the approval
    instead of silently carrying it over to different work. The digest is an
    HMAC under the out-of-store approval key, so it cannot be computed from the
    store alone. With no key there is no valid fingerprint and nothing is
    approved. (A same-user process that reads the key can still forge one; see
    the limitation note at AWAITING_APPROVAL.)
    """
    key = _approval_key(create=create_key)
    if not key:
        return ""
    text = f"{task.get('title') or ''}\n{task.get('description') or ''}"
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def _has_valid_approval(task: dict) -> bool:
    """An approval is valid for one run of the exact text that was approved.

    It is single-use: once a run has consumed it, a requeue of the same task
    (after a failure, a stale in-progress record, a capacity wall) needs a fresh
    human decision, because a high-risk mutation may already be half applied.
    """
    approval = task.get("approval")
    return (
        isinstance(approval, dict)
        and approval.get("status") == "approved"
        and not approval.get("consumed_at")
        and bool(approval.get("fingerprint"))
        and hmac.compare_digest(str(approval.get("fingerprint")), _approval_fingerprint(task))
    )


def _find_awaiting_approval(task_id: str) -> tuple[dict, Path]:
    """Exact-id lookup of a task parked for approval. No substring matching:
    approving the wrong task because an id prefix matched is not acceptable."""
    clean_id = task_id.strip()
    if not re.fullmatch(r"task-[0-9a-f]{8}", clean_id):
        raise ValueError(f"'{task_id}' is not an exact task-xxxxxxxx identifier")
    path = ESCALATED_DIR / f"{clean_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"task {clean_id} is not awaiting approval (not in escalated/)")
    with open(path, "r", encoding="utf-8") as handle:
        task = json.load(handle)
    if task.get("stage_state") != AWAITING_APPROVAL:
        raise ValueError(
            f"task {clean_id} is not awaiting approval (stage_state={task.get('stage_state')!r})"
        )
    return task, path


def _approver_identity() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER") or "unknown"


def approve_task(task_id: str, note: str = "", approved_by: str = "", via: str = "brain swarm approve") -> dict:
    """Record a human approval and move the task back to pending.

    Callers must be the explicit human command; the CLI refuses to call this
    without an interactive terminal.
    """
    task, source = _find_awaiting_approval(task_id)
    now = datetime.now(timezone.utc).isoformat()
    task["approval"] = {
        "status": "approved",
        "approved_by": approved_by or _approver_identity(),
        "approved_at": now,
        "note": note,
        "via": via,
        "fingerprint": _approval_fingerprint(task, create_key=True),
    }
    task.setdefault("approval_history", []).append({"event": "approved", "at": now, "by": task["approval"]["approved_by"], "note": note})
    for stale in ("error", "output", "completed_at", "duration_seconds"):
        task.pop(stale, None)
    task.update({"status": "pending", "stage_state": "approved", "updated_at": now})
    _write_json_atomic(PENDING_DIR / source.name, task)
    source.unlink()
    return task


def reject_task(task_id: str, note: str = "", rejected_by: str = "", via: str = "brain swarm reject") -> dict:
    """Record a human rejection. The task stays in escalated/ and never runs."""
    task, source = _find_awaiting_approval(task_id)
    now = datetime.now(timezone.utc).isoformat()
    who = rejected_by or _approver_identity()
    task["approval"] = {"status": "rejected", "rejected_by": who, "rejected_at": now, "note": note, "via": via}
    task.setdefault("approval_history", []).append({"event": "rejected", "at": now, "by": who, "note": note})
    task.update({
        "status": "escalated",
        "stage_state": "rejected",
        "error": f"rejected by {who}" + (f": {note}" if note else "") + "; not executed",
        "updated_at": now,
    })
    _write_json_atomic(source, task)
    return task


def _hold_for_approval(task: dict, rationale: str) -> dict:
    """Park a task that needs approval. Nothing is executed and no sandbox is made."""
    task_id = task["id"]
    now = datetime.now(timezone.utc).isoformat()
    reason = (
        f"not executed: {rationale or 'high-risk mutation requires approval before execution'}. "
        f"Approve with `brain swarm approve {task_id}` or reject with `brain swarm reject {task_id}`."
    )
    task.update({
        "status": "escalated",
        "stage_state": AWAITING_APPROVAL,
        "requires_approval": True,
        "error": reason,
        "output": reason,
        "updated_at": now,
    })
    previous = task.get("approval")
    if isinstance(previous, dict) and previous.get("status") == "approved":
        # An approval exists but cannot be used: either a previous run already
        # consumed it, or the task text changed after it was given.
        why = (
            "the previous approval was used by an earlier run"
            if previous.get("consumed_at")
            else "the approval no longer matches the task text"
        )
        previous["status"] = "invalidated"
        previous["invalidated_at"] = now
        task.setdefault("approval_history", []).append({"event": "invalidated", "at": now, "by": "swarm", "note": why})
        task["error"] = f"{why}; {reason}"
        task["output"] = task["error"]
    task.pop("started_at", None)
    try:
        _write_json_atomic(ESCALATED_DIR / f"{task_id}.json", task)
        for folder in (PENDING_DIR, IN_PROGRESS_DIR):
            stale = folder / f"{task_id}.json"
            if stale.exists():
                stale.unlink()
    except OSError as error:
        print(f"Error parking task {task_id} for approval: {error}", file=sys.stderr)
    return task


# ------------------------------------------------------------------------------
# Cline capacity resilience
# ------------------------------------------------------------------------------
# Cline runs a single free Gemini model, and Gemini's free tier regularly answers
# "This model is currently experiencing high demand ... try again later". That
# is transient, so it gets bounded retries with backoff before anything else.
# Hard bounds on the operator-supplied retry schedule. The value is read from the
# environment, so a typo ("15,45,..." pasted 200 times, "1e12", "inf") must not
# turn into a retry storm or a worker that sleeps forever.
CLINE_MAX_RETRIES = 4
CLINE_MAX_RETRY_DELAY = 300.0


def _float_list(value: str, default: tuple[float, ...]) -> tuple[float, ...]:
    """Parse "15,45" into (15.0, 45.0). An empty value means no retries.

    Each delay is clamped to [0, CLINE_MAX_RETRY_DELAY] (NaN entries are
    dropped) and at most CLINE_MAX_RETRIES delays are kept.
    """
    try:
        parsed = [float(v) for v in value.split(",") if v.strip()]
    except ValueError:
        return default
    delays = [min(max(0.0, v), CLINE_MAX_RETRY_DELAY) for v in parsed if v == v]  # v != v only for NaN
    return tuple(delays[:CLINE_MAX_RETRIES])


CLINE_RETRY_DELAYS = _float_list(os.environ.get("BRAIN_SWARM_CLINE_RETRY_DELAYS", "15,45"), (15.0, 45.0))
# If cline is still out of capacity after its retries, the task is rerouted to a
# headless agent that does NOT draw on cline's quota. cline and antigravity-api
# authenticate with the same Gemini API key (see CLINE_PROVIDER above), so a
# capacity wall on one is very likely a wall on the other: antigravity-api is
# never a reroute target, and the Antigravity OAuth pool is used with its
# API-key account filtered out.
CLINE_SHARED_KEY_AGENTS = frozenset({"antigravity-api"})


def cline_reroute_agents(raw: str) -> tuple[str, ...]:
    """Parse the reroute order, dropping cline itself and every shared-key agent.

    The filter is applied here rather than trusted to configuration, so no
    value of BRAIN_SWARM_CLINE_REROUTE can send a cline capacity wall onto the
    same Gemini key.
    """
    return tuple(
        agent for agent in (a.strip() for a in raw.split(","))
        if agent and agent != "cline" and agent not in CLINE_SHARED_KEY_AGENTS
    )


CLINE_REROUTE_AGENTS = cline_reroute_agents(os.environ.get("BRAIN_SWARM_CLINE_REROUTE", "kiro-cli,antigravity"))
# Provider phrasing for "busy, come back later", e.g. cline's real stderr
# "error: This model is currently experiencing high demand ... try again later".
# Deliberately narrower than is_capacity_error(): that regex matches a bare
# "429", "capacity", "quota", "rate limit" or "try again later" anywhere, which
# also appears in ordinary failures ("worker.js line 1429", "capacity must be
# > 0", "KeyError: quota_id", "rate limiter test"). Misreading one of those as a
# capacity wall re-runs a genuinely failed task on cline and then again on
# kiro-cli, in the same sandbox and on top of its partial edits. Status codes
# only count as a status ("HTTP 429", "status: 503"), never as a bare number.
_CLINE_TRANSIENT_RE = re.compile(
    r"(high demand|currently unavailable|service unavailable|temporarily (unavailable|overloaded)|"
    r"\bmodel is (currently )?overloaded|\boverloaded_error\b|\bresource[_ ]exhausted\b|\btoo many requests\b|"
    r"\brate limit (exceeded|reached)\b|exceeded your current quota|\bquota exceeded\b|"
    r"\b(status|code|http|error)\W{0,3}(429|503)\b)",
    re.IGNORECASE,
)
# gRPC's status name, matched case-sensitively so prose "unavailable" does not count.
_CLINE_GRPC_UNAVAILABLE_RE = re.compile(r"\bUNAVAILABLE\b")
# Billing/entitlement walls are not transient: retrying them just waits.
_CLINE_BILLING_SIGNATURES = ("No access to", "Insufficient balance", "This model is unavailable for free")
# cline 3.0.x dispatches its built-in lifecycle hooks (agent_start, tool_call,
# tool_result) to its local hub over the `session.hook` RPC, and the hub rejects
# the payload. The client catches the error and prints it unconditionally; it is
# not fatal and has nothing to do with the task. The only switch that disables
# hook dispatch is the hidden `--yolo` mode, which also strips most tools, so the
# message is filtered out of recorded errors instead of changing the invocation.
# Only the message itself is removed: if cline prints it on the same line as a
# real error ("Error: 503 Service Unavailable (hook dispatch failed: ...)"),
# the real error survives. A line left with nothing but a level prefix is dropped.
_CLINE_HOOK_NOISE_RE = re.compile(
    r"\(?\s*hook dispatch failed: session\.hook requires a valid hook event payload\.?\s*\)?",
    re.IGNORECASE,
)
_CLINE_EMPTY_LINE_RE = re.compile(r"^\W*(?:(?:error|warn(?:ing)?|hooks?|cline)\W*)*$", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean_cline_error(text: str) -> str:
    kept = []
    for line in _ANSI_RE.sub("", text or "").splitlines():
        if _CLINE_HOOK_NOISE_RE.search(line):
            line = _CLINE_HOOK_NOISE_RE.sub("", line).rstrip()
            if _CLINE_EMPTY_LINE_RE.match(line):
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_cline_transient_error(text: str) -> bool:
    """Whether cline's *error stream* says the provider is busy.

    Callers pass stderr only, never the agent's stdout: stdout is the task's
    own work and may legitimately talk about quotas, 429s or capacity.
    """
    if any(signature in (text or "") for signature in _CLINE_BILLING_SIGNATURES):
        return False
    return bool(_CLINE_TRANSIENT_RE.search(text or "")) or bool(_CLINE_GRPC_UNAVAILABLE_RE.search(text or ""))


def _run_kiro(task: dict, prompt: str, selection, work_dir: Path) -> tuple[str, str, int]:
    """Run headless Kiro CLI under the task-appropriate model."""
    cmd = [
        str(KIRO_CLI_BIN),
        "chat",
        "--no-interactive",
        "--trust-all-tools",
    ]
    if selection is not None:
        cmd.append(f"--model={selection.model}")
        task["model"] = selection.model
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TASK_TIMEOUT_SECONDS,
        cwd=str(work_dir),
    )
    output_text = proc.stdout.strip()
    error_text = proc.stderr.strip()
    # Kiro reports consumed credits on stderr, not stdout.
    credits = _captured_credits(f"{output_text}\n{error_text}")
    if credits is not None:
        task["credits_consumed"] = credits
    return output_text, error_text, proc.returncode


def _run_antigravity(task: dict, agent: str, prompt: str, selection, work_dir: Path,
                     *, exclude_kinds: tuple[str, ...] = ()) -> tuple[str, str, int]:
    """Run Antigravity across its account pool.

    Try each authenticated account in turn, and within an account walk down the
    ranked fallback models when the wall is capacity rather than a broken task.
    --effort is only sent when the model in hand accepts it, because unsupported
    effort is a hard error. Accounts are isolated by --app_data_dir so one
    account's session is never disturbed by another's. ``exclude_kinds`` drops
    account kinds entirely (the cline reroute excludes the shared "api" key).
    """
    output_text = error_text = ""
    exit_code = 0
    attempts: list[str] = []
    served_by = ""
    proc = None
    stop = False
    approved_once = bool((task.get("approval") or {}).get("consumed_at"))
    done = False
    combined = ""
    for account in antigravity_accounts(agent):
        if done:
            break
        if account.kind in exclude_kinds:
            attempts.append(f"{account.name}: skipped (shares cline's API key)")
            continue
        usable, basis = account_available(account)
        if not usable:
            attempts.append(f"{account.name}: skipped ({basis})")
            continue
        extra_env, prep_error = prepare_account(account)
        if prep_error:
            attempts.append(f"{account.name}: skipped ({prep_error})")
            continue

        _model, _effort, mode = model_for_account(account, selection)
        for model, effort in capacity_fallback_plan(agent, account, selection):
            # Record what is actually sent, not what the policy proposed, so
            # the task evidence cannot disagree with the run.
            if model:
                task["model"] = model
                task["effort"] = effort or None
                task["mode"] = mode or None
            cmd = [
                str(ANTIGRAVITY_BIN),
                "--dangerously-skip-permissions",
                f"--app_data_dir={account.data_dir}",
            ]
            if model:
                cmd.append(f"--model={model}")
                if effort:
                    cmd.append(f"--effort={effort}")
                if mode:
                    cmd.append(f"--mode={mode}")
            cmd.extend(["-p", prompt])
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=TASK_TIMEOUT_SECONDS,
                cwd=str(work_dir),
                env={**os.environ, **extra_env} if extra_env else None,
            )
            output_text = proc.stdout.strip()
            error_text = proc.stderr.strip()
            exit_code = proc.returncode
            served_by = account.name
            combined = f"{output_text}\n{error_text}"
            if exit_code == 0 and not is_capacity_error(combined):
                attempts.append(f"{account.name}: ran on {model or 'default model'}")
                done = True
                break

            if is_capacity_error(combined):
                reason = "out of capacity"
            elif is_opaque_error(combined):
                reason = "opaque agent failure, retryable"
            else:
                reason = f"exit {exit_code}"
            attempts.append(f"{account.name}: {reason} on {model or 'default model'}")

            # One approval authorises one execution. An approved high-risk task
            # that already produced output may have partly run, so it is not
            # re-run on another model or account.
            if approved_once and output_text:
                attempts.append(f"{account.name}: approved high-risk task produced output; not re-run elsewhere")
                stop = True
                break

            # Only a capacity wall is model-specific, so only it justifies
            # downgrading. An opaque failure is retried on the other
            # account, and a diagnosable task error stops everything.
            if not is_capacity_error(combined):
                break
            exit_code = exit_code or 1

        if done or stop:
            break
        retry = should_try_next_account(combined)
        if not retry:
            break
        exit_code = exit_code or 1
    task["antigravity_account"] = served_by
    task["antigravity_attempts"] = attempts
    record_model_downgrade(task, selection)
    if proc is None:
        error_text = "no usable Antigravity account: " + "; ".join(attempts)
        exit_code = 1
    return output_text, error_text, exit_code


def _run_cline_once(task: dict, prompt: str, selection, work_dir: Path) -> tuple[str, str, int]:
    """One headless cline invocation.

    Act mode with auto-approve is the documented non-interactive form;
    --thinking is its effort axis.

    The provider is pinned explicitly. Cline's default provider bills Cline
    Credits, that balance is $0.00 on this account, and the effective provider
    otherwise comes from mutable persisted state in ~/.cline/data/globalState.json
    which any interactive session can change under us. Passing -P makes each run
    independent of that state so a swarm task cannot silently fall back to a paid
    provider.
    """
    cmd = [str(CLINE_BIN), "--auto-approve", "true", "-t", str(max(30, TASK_TIMEOUT_SECONDS - 30))]
    # Correct any drift in cline's persisted provider/model before running.
    # This state is mutable by any interactive session and was observed back
    # on the paid `cline` provider on 2026-09-22, so it is re-pinned here
    # rather than assumed.
    for change in pin_cline_free_route():
        print(f"   [cline] pinned to free route: {change}")
    provider = CLINE_FREE_PROVIDER if CLINE_ENFORCE_FREE else CLINE_PROVIDER
    if provider:
        cmd.extend(["-P", provider])
    # A model is always sent. Omitting it would let cline fall back to its
    # persisted choice, which is the paid-drift path this guards against.
    model = cline_free_model(selection.model if selection is not None else "")
    if model:
        cmd.append(f"--model={model}")
        task["model"] = model
    if selection is not None and selection.effort:
        cmd.append(f"--thinking={selection.effort}")
    cmd.append(prompt)
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TASK_TIMEOUT_SECONDS,
        cwd=str(work_dir),
    )
    output_text = proc.stdout.strip()
    error_text = _clean_cline_error(proc.stderr)
    exit_code = proc.returncode
    # Cline reports model-access and billing problems on stdout with exit
    # 0 in some builds, so a successful exit code alone is not evidence
    # the task ran. "Insufficient balance" is the $0.00 Cline Credits
    # wall, which otherwise gets recorded as a completed empty task.
    combined = f"{output_text}\n{error_text}"
    for signature in _CLINE_BILLING_SIGNATURES:
        if signature in combined:
            exit_code = 1
            error_text = (error_text or output_text)[:500]
            break
    # A capacity message with nothing else to show is a failed run even when
    # cline exits 0.
    if exit_code == 0 and not output_text and is_cline_transient_error(error_text):
        exit_code = 1
    return output_text, error_text, exit_code


def _run_cline_resilient(task: dict, prompt: str, selection, work_dir: Path) -> tuple[str, str, int, str]:
    """Run cline with bounded retries on capacity, then reroute, then escalate.

    Returns (stdout, stderr, exit code, agent that produced the result). Every
    attempt is recorded on ``task["cline_attempts"]`` so the path a task took is
    evidence, not inference. Only a transient capacity error is retried or
    rerouted; any other failure stops immediately, because rerunning a broken
    task on another agent just burns more capacity on the same error.
    """
    attempts: list[str] = []
    task["cline_attempts"] = attempts
    delays = CLINE_RETRY_DELAYS[:CLINE_MAX_RETRIES]
    total = len(delays) + 1
    # An approved high-risk task authorises one execution. A retry or reroute
    # after an attempt that already produced output could apply the mutation
    # twice, so for those tasks only an attempt that never got going (no
    # stdout at all) may be repeated.
    approved_once = bool((task.get("approval") or {}).get("consumed_at"))
    output_text, error_text, exit_code = "", "", 1
    for attempt in range(total):
        output_text, error_text, exit_code = _run_cline_once(task, prompt, selection, work_dir)
        model = task.get("model") or "default model"
        if exit_code == 0:
            attempts.append(f"cline: ran on {model} (attempt {attempt + 1}/{total})")
            return output_text, error_text, exit_code, "cline"
        # The capacity decision reads cline's error stream only. stdout is the
        # agent's own work and may legitimately mention "capacity" or "HTTP 429";
        # an empty stderr means no provider error, so the failure is final.
        if not is_cline_transient_error(error_text):
            attempts.append(f"cline: exit {exit_code} on {model}, not a capacity error; not retried")
            return output_text, error_text, exit_code, "cline"
        if approved_once and output_text:
            attempts.append(
                f"cline: out of capacity on {model} after producing output (attempt {attempt + 1}/{total}); "
                f"approved high-risk task not re-run"
            )
            return output_text, (
                f"{error_text}\n(not retried or rerouted: this approved high-risk task may have partly run; "
                f"re-approve to run it again)"
            ), exit_code, "cline"
        attempts.append(f"cline: out of capacity on {model} (attempt {attempt + 1}/{total})")
        if attempt < len(delays):
            delay = delays[attempt]
            attempts.append(f"cline: waiting {delay:g}s before retry")
            time.sleep(delay)

    cline_error = error_text
    cline_record = {field: task.get(field) for field in ("model", "effort", "mode")}
    # Cline is exhausted. Reroute to a headless agent that does not share its key.
    for target in CLINE_REROUTE_AGENTS:
        if target in CLINE_SHARED_KEY_AGENTS or target == "cline":  # defensive; filtered at load time
            attempts.append(f"reroute {target}: skipped (shares cline's Gemini API key or is cline)")
            continue
        # Forget cline's model so the record names only what the target sent.
        for field in ("model", "effort", "mode"):
            task.pop(field, None)
        if target == "kiro-cli":
            if not KIRO_CLI_BIN.exists():
                attempts.append("reroute kiro-cli: skipped (not installed)")
                continue
            target_selection = _selection_for(prompt, "kiro-cli")
            out, err, code = _run_kiro(task, prompt, target_selection, work_dir)
        elif target == "antigravity":
            if not ANTIGRAVITY_BIN.exists():
                attempts.append("reroute antigravity: skipped (not installed)")
                continue
            target_selection = _selection_for(prompt, "antigravity")
            out, err, code = _run_antigravity(task, "antigravity", prompt, target_selection, work_dir, exclude_kinds=("api",))
            if err.startswith("no usable Antigravity account"):
                # Nothing ran: every OAuth account was unusable. That is an
                # unavailable route, not this task's result.
                attempts.append(f"reroute antigravity: skipped ({err})")
                continue
        else:
            attempts.append(f"reroute {target}: skipped (not a supported reroute target)")
            continue
        # Whether the target was itself out of capacity is read from its error
        # stream with the narrow provider check, for the same reason as cline's:
        # a real failure whose output mentions "429" or "quota" must be reported,
        # not re-run on the next route on top of its partial edits. An approved
        # high-risk task that produced output is never passed on either.
        target_walled = (
            is_cline_transient_error(err) or is_opaque_error(err) or is_auth_error(err)
        ) and not (approved_once and out)
        if code == 0 and not (not out and target_walled):
            attempts.append(f"reroute {target}: ran on {task.get('model') or 'default model'}")
        elif target_walled:
            attempts.append(f"reroute {target}: out of capacity")
            continue
        else:
            # A diagnosable failure on the reroute target is a real result for
            # this task; report it rather than burning further routes.
            attempts.append(f"reroute {target}: exit {code}, not a capacity error; stopped")
        # Mirror the capability-remap convention: routed_to keeps the router's
        # decision, assigned_to names the worker that actually ran.
        task["routed_to"] = "cline"
        task["rerouted_to"] = target
        task["reroute_reason"] = f"cline out of capacity after {total} attempt(s); rerouted to {target}"
        task["assigned_to"] = target
        return out, err, code, target

    # No route ran the task, so the record keeps describing cline's attempts.
    task.update(cline_record)
    reason = (
        f"cline out of capacity after {total} attempt(s) and every reroute target was exhausted "
        f"({', '.join(CLINE_REROUTE_AGENTS) or 'none configured'}; antigravity-api excluded because "
        f"it shares cline's Gemini API key). Last cline error: {cline_error or 'none'}"
    )
    return output_text, reason, 1, "cline"


def execute_task_worker(task: dict) -> dict:
    """Executes a single task headlessly via the assigned agent."""
    task_id = task["id"]
    agent = task["assigned_to"]
    prompt = task["title"]
    if task["description"]:
        prompt += f"\n\nContext & Instructions:\n{task['description']}"

    # Move from pending to in-progress
    pending_file = PENDING_DIR / f"{task_id}.json"
    in_progress_file = IN_PROGRESS_DIR / f"{task_id}.json"

    task["status"] = "in-progress"
    task["started_at"] = datetime.now(timezone.utc).isoformat()

    selection = _selection_for(prompt, agent)
    if selection is not None:
        task["model"] = selection.model
        task["model_rationale"] = selection.rationale
        if selection.effort:
            task["effort"] = selection.effort
        if selection.mode:
            task["mode"] = selection.mode

    # Approval gate. Runs before the task is claimed, sandboxed or executed, so a
    # held task leaves no in-progress record, worktree or branch behind.
    approval_reason = _approval_reason(prompt, agent, selection)
    approval_gated = bool(approval_reason)
    if approval_gated:
        if not _has_valid_approval(task):
            return _hold_for_approval(task, approval_reason)
        approval = task["approval"]
        # Consume the approval now, before anything runs, so this exact run is
        # the only one it authorises.
        approval["consumed_at"] = datetime.now(timezone.utc).isoformat()
        task["model_rationale"] = (
            f"{task.get('model_rationale') or 'high-risk mutation'}; approved by "
            f"{approval.get('approved_by')} at {approval.get('approved_at')}"
        )

    # Claim the task. The pending -> in-progress rename is atomic, so when two
    # runners loaded the same pending file only one rename succeeds and the
    # loser must not execute. An approved high-risk task additionally must be
    # claimed from pending/: without that, a stale copy of the approved record
    # held by a second runner would spend the single-use approval twice.
    claimed_elsewhere = {
        "id": task_id,
        "title": task.get("title", ""),
        "status": "claimed-elsewhere",
        "note": "not executed here: another runner claimed this task first",
    }
    try:
        if approval_gated or pending_file.exists():
            pending_file.rename(in_progress_file)
    except FileNotFoundError:
        print(f"Task {task_id} was claimed by another runner; not executing it again.", file=sys.stderr)
        return claimed_elsewhere
    except OSError as e:
        print(f"Error moving task {task_id} to in-progress: {e}", file=sys.stderr)
        if approval_gated:
            return {**claimed_elsewhere, "note": f"not executed: could not claim the approved task ({e})"}
    try:
        with open(in_progress_file, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2)
    except Exception as e:
        # Do not silently swallow: a failed in-progress write leaves the queue
        # in an inconsistent state, so surface it rather than hiding it.
        print(f"Error writing in-progress record for task {task_id}: {e}", file=sys.stderr)

    start_t = time.time()
    output_text = ""
    error_text = ""
    exit_code = 0

    # Resolve a guarded working directory before anything runs. Headless agents
    # have no approval gate, so they never see the user's checkout: they get a
    # worktree on a throwaway branch, or no repository access at all.
    sandbox: dict | None = None
    work_dir = BRAIN_DIR
    sandbox_note = ""
    # The API worker is excluded deliberately: it is pure inference over a
    # prompt with no tools and no filesystem access, so it can neither read nor
    # write a checkout. Putting it through repo resolution made it escalate on
    # "ambiguous repository" for questions that never needed a repository.
    if agent not in DELIVERY_AGENTS and agent != API_AGENT and SANDBOX_ENABLED:
        repo, reason = resolve_target_repo(prompt, str(task.get("repo") or ""))
        task["repo_resolution"] = reason
        if repo is None and reason.startswith(("ambiguous", "unknown repository")):
            # Guessing a repository would either do nothing useful or write to
            # the wrong service, so refuse and say why.
            task.update({
                "status": "escalated",
                "error": reason,
                "duration_seconds": round(time.time() - start_t, 3),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "output": f"not executed: {reason}",
            })
            dest = ESCALATED_DIR / f"{task_id}.json"
            try:
                if in_progress_file.exists():
                    in_progress_file.unlink()
                _write_json_atomic(dest, task)
            except OSError:
                pass
            return task
        if repo is not None:
            sandbox, sandbox_note = create_sandbox(task_id, repo)
            if sandbox is None:
                task.update({
                    "status": "escalated",
                    "error": sandbox_note,
                    "duration_seconds": round(time.time() - start_t, 3),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "output": f"not executed: {sandbox_note}",
                })
                dest = ESCALATED_DIR / f"{task_id}.json"
                try:
                    if in_progress_file.exists():
                        in_progress_file.unlink()
                    _write_json_atomic(dest, task)
                except OSError:
                    pass
                return task
            work_dir = Path(sandbox["path"])
            task["sandbox"] = sandbox
            prompt += (
                f"\n\nWorkspace: you are in an isolated git worktree of the "
                f"`{sandbox['repo_name']}` repository at {sandbox['path']}, checked out on branch "
                f"`{sandbox['branch']}`. Edit files there directly; the user's own checkout is a "
                f"different directory and must not be touched. Do not commit, push, or switch "
                f"branches — your changes are committed to this branch for review automatically. "
                f"Finish by stating which files you changed and why."
            )
        else:
            prompt += (
                "\n\nWorkspace: no repository was resolved for this task, so you have no project "
                "checkout. Do not attempt to edit source files; report what you determined instead."
            )

    # ------------------------------------------------------------------
    # Capability remap.
    #
    # Every CLI branch below is gated on its binary existing. On a machine where
    # the routed agent is not installed, the task used to fall through to a
    # branch that fabricated success. So the swarm silently reported completed
    # work that never ran — the exact failure mode when someone else clones this
    # project.
    #
    # A registered provider account is a real worker that needs no CLI and no
    # IDE, so prefer it over pretending. The original routing decision is kept
    # on the task so the remap is visible rather than hidden.
    # ------------------------------------------------------------------
    if agent not in DELIVERY_AGENTS and not _agent_cli_available(agent):
        fallback = _api_fallback_provider()
        if fallback:
            task["routed_to"] = agent
            task["remapped_to"] = API_AGENT
            task["remap_reason"] = (
                f"{agent} CLI is not installed on this host; ran on provider "
                f"account '{fallback}' instead (no CLI required)"
            )
            agent = API_AGENT
            task["assigned_to"] = API_AGENT

    try:
        if agent == API_AGENT:
            # Direct-provider worker. Needs only an API key, so this is the path
            # that makes the swarm usable on a host with no agent CLI at all.
            # Token usage and cost come back from the provider and are recorded
            # on the task rather than estimated.
            output_text, error_text, exit_code = _run_api_worker(task, prompt)

        elif agent == "kiro-cli" and KIRO_CLI_BIN.exists():
            output_text, error_text, exit_code = _run_kiro(task, prompt, selection, work_dir)

        elif agent in ("antigravity", "antigravity-api") and ANTIGRAVITY_BIN.exists():
            output_text, error_text, exit_code = _run_antigravity(task, agent, prompt, selection, work_dir)

        elif agent == "cline" and CLINE_BIN.exists():
            output_text, error_text, exit_code, agent = _run_cline_resilient(task, prompt, selection, work_dir)

        elif agent in DELIVERY_AGENTS:
            # In-editor agents (Cline, Antigravity IDE) have no verified
            # headless entrypoint, so the swarm records a durable delivery and
            # waits for the agent's own acknowledgement.
            output_text, error_text = stage_task_for_delivery(task, agent)
            exit_code = 0 if not error_text else 1

        else:
            # No worker could run this task. Previously this branch wrote
            # "processed and verified by <agent>" with exit 0, which recorded a
            # completed task for work that never happened. Report the real
            # reason and the exact remedy instead.
            error_text = (
                f"No worker available for '{agent}' on this host. Its CLI is not "
                f"installed and no provider account is configured, so nothing ran.\n"
                f"Fix either way:\n"
                f"  - install the agent CLI, or\n"
                f"  - register a provider account: scripts/brain/brain providers add <provider>\n"
                f"Run `scripts/brain/brain doctor` to see what this host can do."
            )
            exit_code = 1

    except subprocess.TimeoutExpired:
        error_text = f"Task execution timed out after {TASK_TIMEOUT_SECONDS} seconds."
        exit_code = 124
    except Exception as e:
        error_text = str(e)
        exit_code = 1

    duration = round(time.time() - start_t, 3)
    task["duration_seconds"] = duration
    task["completed_at"] = datetime.now(timezone.utc).isoformat()
    # Never label a failed run a success: the escalated record used to read
    # "Success (No stdout returned)" next to a capacity error.
    if output_text:
        task["output"] = output_text
    elif exit_code == 0:
        task["output"] = "Completed (exit 0); the agent returned no stdout."
    else:
        task["output"] = f"Failed (exit {exit_code}); the agent returned no stdout. See error."

    # Snapshot whatever the agent changed onto the sandbox branch and tear the
    # worktree down. This runs even on failure, so a partial change is still
    # reviewable rather than lost.
    if sandbox is not None:
        snapshot = finalize_sandbox(sandbox)
        task["sandbox_result"] = snapshot
        if snapshot.get("changed"):
            task["output"] += (
                f"\n\n--- guarded change ---\nRepository: {snapshot['repo']}\n"
                f"Branch: {snapshot['branch']}\nCommit: {snapshot['commit']}\n"
                f"Files changed: {snapshot['files']}\n{snapshot['diffstat']}\n"
                f"Review with: git -C {sandbox['repo']} diff {sandbox['base_commit'][:12]}..{snapshot['branch']}\n"
                f"Discard with: git -C {sandbox['repo']} branch -D {snapshot['branch']}"
            )
        else:
            task["output"] += f"\n\n--- guarded change ---\nNo files were modified in {snapshot['repo']}; no branch kept."

    # A nonzero exit code is not always a real failure: the Antigravity CLI
    # (notably antigravity-api) has been observed writing a complete, correct
    # answer to stdout and *then* exiting 1. Discarding that would escalate good
    # work. So if a headless agent produced substantial output and the failure
    # carries no diagnosable capacity/opaque signature, treat the run as
    # successful and keep the answer. Delivery agents are excluded — their exit
    # code is synthetic — as are empty-output failures, which stay escalated.
    if (
        exit_code != 0
        and agent not in DELIVERY_AGENTS
        and len(output_text.strip()) >= 200
        and not is_capacity_error(f"{output_text}\n{error_text}")
        and not is_opaque_error(f"{output_text}\n{error_text}")
    ):
        task["salvaged_nonzero_exit"] = exit_code
        task["output"] = (
            f"{task['output']}\n\n--- note ---\n"
            f"Agent exited {exit_code} but produced substantial output above; "
            f"kept as completed rather than discarded."
        )
        exit_code = 0

    if exit_code == 0:
        if agent in DELIVERY_AGENTS:
            # Delivered in-editor; stays in-progress until the agent records its
            # own lifecycle evidence. stage_task_for_delivery already persisted
            # the authoritative record, so annotate that instead of replacing it.
            # The staging text is only a delivery *receipt*, not task output, so it
            # is kept in a separate field; writing it into ``output`` would make an
            # unacknowledged delivery permanently exempt from the stale check.
            stored, source, _ = _exact_task(task_id)
            stored.update({"delivery_receipt": output_text, "duration_seconds": duration, "completed_at": None, "updated_at": datetime.now(timezone.utc).isoformat()})
            _write_json_atomic(source, stored)
            return stored
        task["status"] = "completed"
        dest_file = COMPLETED_DIR / f"{task_id}.json"
    else:
        task["status"] = "escalated"
        task["error"] = error_text or f"Process exited with code {exit_code}"
        dest_file = ESCALATED_DIR / f"{task_id}.json"

    # Move to destination
    try:
        if in_progress_file.exists() and dest_file != in_progress_file:
            in_progress_file.unlink()
        with open(dest_file, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2)
    except Exception as e:
        print(f"Error persisting completed task: {e}", file=sys.stderr)

    return task


def complete_task(task_id: str, notes: str = "") -> dict | None:
    """Marks an in-progress, pending, or escalated task as completed."""
    target_file = None
    clean_id = task_id.strip()

    for folder in [IN_PROGRESS_DIR, PENDING_DIR, ESCALATED_DIR]:
        # Exact match
        candidate = folder / f"{clean_id}.json"
        if candidate.exists():
            target_file = candidate
            break
        # Prefix or substring match
        for f in folder.glob("*.json"):
            if clean_id in f.stem:
                target_file = f
                break
        if target_file:
            break

    if not target_file:
        return None

    try:
        with open(target_file, "r", encoding="utf-8") as f:
            task = json.load(f)

        if task.get("stage_state") == AWAITING_APPROVAL:
            # It never ran, so recording it completed would be a false claim.
            print(
                f"Error: task {task.get('id')} is awaiting approval and has not run; use "
                f"`brain swarm approve` or `brain swarm reject` instead.",
                file=sys.stderr,
            )
            return None

        task["status"] = "completed"
        task["completed_at"] = datetime.now(timezone.utc).isoformat()
        if notes:
            task["output"] = (task.get("output") or "") + f"\n\n[Completion Notes]: {notes}"
        else:
            task["output"] = (task.get("output") or "") + "\n\nMarked completed via brain swarm complete."

        dest_file = COMPLETED_DIR / target_file.name
        if target_file != dest_file:
            target_file.unlink()
        with open(dest_file, "w", encoding="utf-8") as f:
            json.dump(task, f, indent=2)

        return task
    except Exception as e:
        print(f"Error completing task: {e}", file=sys.stderr)
        return None


def run_swarm_queue(concurrency: int = 3) -> list[dict]:
    """Runs all pending tasks concurrently up to concurrency limit."""
    pending_files = list(PENDING_DIR.glob("*.json"))
    if not pending_files:
        return []

    pending_tasks = []
    for pf in pending_files:
        try:
            with open(pf, "r", encoding="utf-8") as f:
                pending_tasks.append(json.load(f))
        except Exception:
            pass

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(execute_task_worker, t): t for t in pending_tasks}
        for future in concurrent.futures.as_completed(futures):
            try:
                res = future.result()
                results.append(res)
            except Exception as exc:
                t = futures[future]
                results.append({"id": t["id"], "error": str(exc), "status": "failed"})

    return results


def _print_run_summary(results: list[dict]) -> None:
    """Say what actually happened; a task held for approval did not execute."""
    held = [r for r in results if r.get("stage_state") == AWAITING_APPROVAL]
    skipped = [r for r in results if r.get("status") == "claimed-elsewhere"]
    print(
        f"Processed {len(results)} task(s); {len(results) - len(held) - len(skipped)} executed, "
        f"{len(held)} held for approval" + (f", {len(skipped)} claimed by another runner." if skipped else ".")
    )
    for r in held:
        print(f"  ⏸ [{r['id']}] held, NOT executed: {r.get('title', '')}")
        print(f"      approve: brain swarm approve {r['id']} --note '...'   reject: brain swarm reject {r['id']}")
    for r in results:
        if r.get("rerouted_to"):
            print(f"  ↪ [{r['id']}] {r.get('reroute_reason')}")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Ultra-Fast Autonomous Multi-Agent Swarm (Swarm Mesh)"
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Swarm commands")

    # dispatch
    disp_parser = subparsers.add_parser("dispatch", help="Classify and queue a batch of tasks")
    disp_parser.add_argument("tasks", help="Semicolon or newline separated task list")
    disp_parser.add_argument("--run", action="store_true", help="Immediately execute tasks")
    disp_parser.add_argument("--agent", choices=sorted(AGENT_PROFILES), help="Override assigned agent")
    disp_parser.add_argument("--repo", default="", help="Repository to sandbox the task in (name under the workspace root)")

    # status
    subparsers.add_parser("status", help="Show swarm queue status and active agents")

    # run
    run_parser = subparsers.add_parser("run", help="Execute all pending tasks in the queue")
    run_parser.add_argument("--concurrency", type=int, default=3, help="Max parallel workers")

    # complete
    comp_parser = subparsers.add_parser("complete", help="Mark an in-progress or staged task as completed")
    comp_parser.add_argument("task_id", help="Task ID or substring match")
    comp_parser.add_argument("--notes", "-n", default="", help="Optional completion notes")

    # approve / reject: the only way a task held for approval can proceed.
    appr_parser = subparsers.add_parser("approve", help="Approve a high-risk task held for approval and re-queue it")
    appr_parser.add_argument("task_id", help="Exact task-xxxxxxxx identifier")
    appr_parser.add_argument("--note", "-n", default="", help="Why it is approved (recorded on the task)")
    rej_parser = subparsers.add_parser("reject", help="Reject a task held for approval; it will never run")
    rej_parser.add_argument("task_id", help="Exact task-xxxxxxxx identifier")
    rej_parser.add_argument("--note", "-n", default="", help="Why it is rejected (recorded on the task)")

    # Durable in-editor delivery lifecycles (Cline, Antigravity IDE)
    for delivery_agent in DELIVERY_AGENTS:
        agent_parser = subparsers.add_parser(delivery_agent, help=f"Record verifiable {delivery_agent} delivery lifecycle events")
        agent_subparsers = agent_parser.add_subparsers(dest=f"{delivery_agent}_command", required=True)
        for command in ("acknowledge", "progress", "awaiting-verification", "complete", "escalate"):
            lifecycle = agent_subparsers.add_parser(command)
            lifecycle.add_argument("task_id", help="Exact task-xxxxxxxx identifier")
            lifecycle.add_argument("--note", "-n", default="", help="Progress, completion, or escalation evidence")
            lifecycle.add_argument("--session", default="", help=f"{delivery_agent} session identifier")
            lifecycle.add_argument("--model", default="", help="The model you actually switched to, recorded as reported evidence")
        agent_subparsers.add_parser("status", help=f"Show evidence-qualified {delivery_agent} lifecycle state")

    # clear
    subparsers.add_parser("clear", help="Clear completed and escalated task history")

    args = parser.parse_args()

    if args.subcommand == "dispatch":
        raw_items = [
            t.strip()
            for t in re.split(r"[;\n]+", args.tasks)
            if t.strip() and not t.strip().startswith("#")
        ]
        created = []
        print(f"\n⚡ Swarm Router analyzing {len(raw_items)} task(s)...")
        for item in raw_items:
            # Support Title :: Description syntax
            if "::" in item:
                title, desc = item.split("::", 1)
            else:
                title, desc = item, ""
            task = create_task(title, desc, preferred_agent=args.agent, repo=getattr(args, 'repo', ''))
            created.append(task)
            print(
                f"  ✓ [{task['id']}] -> {task['assigned_to'].upper():<12} (Conf: {task['confidence']*100:.0f}%): {task['title']}"
            )

        if args.run:
            print(f"\n🚀 Launching Swarm Worker Pool (Parallel execution)...")
            results = run_swarm_queue()
            _print_run_summary(results)

    elif args.subcommand == "run":
        print(f"🚀 Running Swarm Queue with concurrency {args.concurrency}...")
        results = run_swarm_queue(concurrency=args.concurrency)
        _print_run_summary(results)

    elif args.subcommand == "complete":
        res = complete_task(args.task_id, notes=args.notes)
        if res:
            print(f"✓ Task [{res['id']}] marked COMPLETED: {res['title']}")
        else:
            print(f"Error: Task '{args.task_id}' not found in active queues.", file=sys.stderr)
            sys.exit(1)

    elif args.subcommand in ("approve", "reject"):
        # Approval is a human decision. Headless agents always run with stdin
        # detached (subprocess.DEVNULL), so requiring a terminal keeps an agent,
        # a hook or a notification responder from approving work on anyone's
        # behalf (decisions/desktop-notifications-must-never-record-approval).
        # The terminal check alone does not prove a human (any process can
        # allocate a pty), so a decision is also refused when an agent CLI is
        # among this process's ancestors. Both are deterrents against an agent
        # approving work, not a proof of a person; see the note at
        # AWAITING_APPROVAL.
        if not sys.stdin.isatty():
            print(
                f"Error: `brain swarm {args.subcommand}` must be run by a human at an interactive "
                f"terminal; refusing to record a decision without one.",
                file=sys.stderr,
            )
            sys.exit(2)
        agent_parent = _agent_ancestor()
        if agent_parent:
            print(
                f"Error: `brain swarm {args.subcommand}` was launched from inside an agent "
                f"({agent_parent}); approval decisions must be typed by a human in a plain terminal.",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            tty = os.ttyname(sys.stdin.fileno())
        except OSError:
            tty = "tty"
        via = f"brain swarm {args.subcommand} ({tty})"
        try:
            if args.subcommand == "approve":
                res = approve_task(args.task_id, note=args.note, via=via)
                print(f"✓ Task [{res['id']}] APPROVED by {res['approval']['approved_by']} and re-queued: {res['title']}")
                print("  Run it with: brain swarm run")
            else:
                res = reject_task(args.task_id, note=args.note, via=via)
                print(f"✗ Task [{res['id']}] REJECTED by {res['approval']['rejected_by']}; it will not run: {res['title']}")
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)

    elif args.subcommand in DELIVERY_AGENTS:
        delivery_agent = args.subcommand
        subcommand = getattr(args, f"{delivery_agent}_command")
        if subcommand == "status":
            print(json.dumps(_delivery_state(get_all_tasks(), delivery_agent), indent=2))
            return
        operations = {
            "acknowledge": delivery_acknowledge,
            "progress": delivery_progress,
            "awaiting-verification": delivery_awaiting_verification,
            "complete": delivery_complete,
            "escalate": delivery_escalate,
        }
        try:
            if subcommand in {"acknowledge", "complete"}:
                result = operations[subcommand](delivery_agent, args.task_id, note=args.note, session=args.session, model=args.model)
            else:
                result = operations[subcommand](delivery_agent, args.task_id, args.note, session=args.session, model=args.model)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"agent": delivery_agent, "task_id": args.task_id, "lifecycle": result["delivery"]["status"], "model_reported": result["delivery"].get("model_reported"), "updated_at": result["delivery"]["updated_at"]}, indent=2))

    elif args.subcommand == "clear":
        kept = 0
        for folder in [COMPLETED_DIR, ESCALATED_DIR]:
            for f in folder.glob("*.json"):
                # A task awaiting approval is an open human decision, not
                # history: deleting it would be a silent, unrecorded rejection.
                if folder == ESCALATED_DIR and _load_json(f).get("stage_state") == AWAITING_APPROVAL:
                    kept += 1
                    continue
                f.unlink()
        print("Cleared completed and escalated tasks.")
        if kept:
            print(f"Kept {kept} task(s) awaiting approval; approve or reject them explicitly.")

    else:
        # Default: status
        tasks = get_all_tasks()
        total = sum(len(v) for v in tasks.values())
        ide_state = _delivery_state(tasks, "antigravity-ide")
        print("==================================================================")
        print("  AUTONOMOUS MULTI-AGENT SWARM MESH STATUS")
        print("==================================================================")
        print(f"Active Agents in Mesh:")
        print(f"  🧠 Antigravity (Master Architect) : {'ONLINE' if ANTIGRAVITY_BIN.exists() else 'OFFLINE'}")
        if ANTIGRAVITY_BIN.exists():
            # Antigravity is a pool of accounts; show which have usable credentials
            # so an exhausted or unattached account is visible before a run.
            for account in antigravity_accounts():
                usable, basis = account_available(account)
                scope = "gemini only" if account.models_allowed else "all models"
                print(f"       └ account {account.name:8} [{account.kind:5}] "
                      f"{'READY' if usable else 'NOT ATTACHED':12} ({scope}) {basis}")
        print(f"  ⚡ Kiro CLI (Terminal Workhorse)  : {'ONLINE' if KIRO_CLI_BIN.exists() else 'OFFLINE'}")
        print(f"  💻 Cline (Headless Coding CLI)    : {'ONLINE' if CLINE_BIN.exists() else 'OFFLINE'}")
        print(f"  🛰️ Antigravity IDE (In-Editor)    : {ide_state['availability'].upper()} ({ide_state['basis']})")
        print("------------------------------------------------------------------")
        print(f"Queue Stats (Total: {total}):")
        print(f"  ⏳ Pending     : {len(tasks['pending'])}")
        print(f"  ⚙️ In-Progress : {len(tasks['in-progress'])}")
        print(f"  🕒 Stale       : {len(tasks['stale'])}")
        awaiting = [t for t in tasks["escalated"] if t.get("stage_state") == AWAITING_APPROVAL]
        print(f"  ✓ Completed   : {len(tasks['completed'])}")
        print(f"  ⚠️ Escalated   : {len(tasks['escalated'])} (awaiting approval: {len(awaiting)})")
        print("------------------------------------------------------------------")
        # Token cost account. Only provider-API tasks expose real usage; agent
        # CLIs bill inside their own accounts, so they are reported as opaque
        # rather than given a fabricated number.
        acct = token_account(tasks)
        tot = acct["totals"]
        print("Token Cost Account:")
        if acct["by_provider"]:
            print(f"  metered via provider API : {tot['total_tokens']:,} tokens "
                  f"(in {tot['prompt_tokens']:,} / out {tot['completion_tokens']:,})"
                  f"  ${tot['cost_usd']:.6f}")
            for row in acct["by_provider"]:
                models = ", ".join(sorted(row["models"]))
                print(f"    - {row['provider']:<12} {row['tasks']:>3} tasks  "
                      f"{row['total_tokens']:>8,} tok  ${row['cost_usd']:.6f}  "
                      f"[{row['billing']}]  {models}")
            print(f"  free-tier tasks          : {tot['free_tasks']}"
                  f"   billed tasks: {tot['metered_tasks']}")
        else:
            print("  no provider-API tasks recorded yet (0 tokens, $0.000000)")
        if acct["opaque"]["tasks"]:
            agents = ", ".join(f"{a}×{n}" for a, n in sorted(acct["opaque"]["agents"].items()))
            print(f"  agent-CLI tasks (usage not exposed by the CLI): "
                  f"{acct['opaque']['tasks']}  [{agents}]")
        print("------------------------------------------------------------------")
        if tasks["stale"]:
            print("Stale Tasks (in-progress, no heartbeat/output/error/completion evidence):")
            for t in tasks["stale"][:5]:
                reason = t.get("stale_reason", "no recent evidence")
                print(f"  - [{t['id']}] ({t['assigned_to']}): {t['title']} — {reason}")
        if awaiting:
            print("Awaiting Approval (high-risk; will not run until a human approves):")
            for t in awaiting[:10]:
                print(f"  - [{t['id']}] ({t['assigned_to']}): {t['title']}")
            print("    approve: brain swarm approve <task-id> [--note ...]   reject: brain swarm reject <task-id>")
        if tasks["in-progress"]:
            print("In-Progress / Staged Tasks:")
            for t in tasks["in-progress"][:5]:
                extra = f" [DELIVERED TO {t['assigned_to'].upper()}: {t.get('stage_state', 'staged')}]" if t.get("assigned_to") in DELIVERY_AGENTS else ""
                print(f"  - [{t['id']}] ({t['assigned_to']}): {t['title']}{extra}")
        if tasks["pending"]:
            print("Next Pending Tasks:")
            for t in tasks["pending"][:3]:
                print(f"  - [{t['id']}] ({t['assigned_to']}): {t['title']}")
        if tasks["completed"]:
            print("Recently Completed:")
            for t in tasks["completed"][:3]:
                print(f"  - [{t['id']}] ({t['assigned_to']}): {t['title']} ({t.get('duration_seconds', 0)}s)")
        print("==================================================================")


if __name__ == "__main__":
    main()
