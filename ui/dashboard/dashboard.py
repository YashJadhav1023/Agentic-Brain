#!/usr/bin/env python3
"""Mission Control Dashboard Server.

Next-Gen Shared Brain & Multi-Agent Mission Control web application.
Serves interactive tabs: Overview, Agents, Tasks Kanban, Execution Flow,
Routing Simulator & History, Memory Explorer, Handoff Viewer, Worktree Sandboxes,
Live Events, and Git Monitor.
Equipped with local security hardening: Bearer token auth for mutating actions,
rate limiting, CORS local-origin restriction, security headers, and worktree approval gates.
"""
from __future__ import annotations

import atexit
import base64
import datetime
import errno
import hmac
import json
import logging
import os

logger = logging.getLogger("MissionControl.Dashboard")
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import functools
import threading
import time
import urllib.parse
import urllib.request
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.base.adapter import Capability
from brain.analytics import (
    get_analytics_engine,
    get_cost_tracker,
    get_quota_manager,
    get_retention_manager,
    get_usage_tracker,
)
from brain.context.continuator import UniversalContinuator
from brain.orchestrator.job import Job
from brain.orchestrator.job_manager import JobManager, UsageTracker
from brain.orchestrator.orchestrator import Orchestrator
from brain.router.models import RoutingMode
from brain.router.smart_router import SmartRouter
from events.bus import Event, EventBus, EventType
from handoffs.handoff_manager import HandoffManager
from memory.retrieval.retriever import MemoryRetriever
from memory.store.memory_store import MemoryScope, MemoryStore
from models.policies.model_policy import Complexity
from providers.api.anthropic_native import AnthropicProvider
from providers.api.gemini_native import GeminiProvider
from providers.api.ollama_local import OllamaProvider
from providers.api.openai_compatible import OpenAICompatibleProvider
from providers.base import ProviderType
from providers.registry.account_registry import Account, AccountStatus, AuthenticationType
from providers.registry.bootstrap import create_default_registry, sync_registry_with_config
from providers.registry.config import load_config, resolve_config_path
from providers.registry.credential_manager import SecretRedactor, get_credential_manager
from providers.registry.model_registry import ModelMetadata
from tasks.manager import Task, TaskManager, TaskPriority, TaskStatus
from ui.dashboard import office_state

STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()
PORT = int(os.environ.get("BRAIN_PORT", "3333"))
#: Shown when an account's email cannot be read from the user's own local agent
#: session. Deliberately not a real address.
UNKNOWN_ACCOUNT_EMAIL = "unknown@localhost"

def brain_dir() -> Path:
    """Root of the shared-brain store on THIS machine.

    Honours BRAIN_DIR so a user can relocate their own data; defaults to
    ~/agentic-brain, which is the path the project has always used. Previously
    BRAIN_DIR was documented in .env.example but read by no code, and the documented
    value (~/.agentic-brain) did not match the hardcoded one.
    """
    return Path(os.environ.get("BRAIN_DIR", "")).expanduser() if os.environ.get("BRAIN_DIR") \
        else Path.home() / "agentic-brain"



def _js_literal(value: Any) -> str:
    """Encode a value as a JS literal that is safe inside an inline <script>.

    json.dumps alone leaves ``</script>`` intact, and html.escape does not stop
    a backslash from escaping the closing quote, so neither is safe for
    attacker-influenced strings (OAuth callback query parameters, emails).
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


_STALE_WORKTREE_STATUSES = frozenset({"APPLIED", "REJECTED", "FAILED", "ORPHANED"})


def _cleanup_stale_worktrees(manager: Any, max_age_hours: int) -> list[dict[str, Any]]:
    """Clean finished/abandoned worktrees last updated more than max_age_hours ago."""
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=max(0, max_age_hours))
    records = manager.status() or []
    if not isinstance(records, list):
        records = [records]
    cleaned: list[dict[str, Any]] = []
    for rec in records:
        status = getattr(rec.status, "value", rec.status)
        if status not in _STALE_WORKTREE_STATUSES:
            continue
        try:
            updated = datetime.datetime.fromisoformat(str(rec.updated_at).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if updated > cutoff:
            continue
        # Keep the branch: a FAILED/ORPHANED sandbox may still hold commits worth
        # inspecting. Only the on-disk worktree directory is reclaimed.
        if manager.cleanup(rec.task_id, force=True, delete_branch=False):
            cleaned.append({"task_id": rec.task_id, "previous_status": status})
    return cleaned


class BadRequest(ValueError):
    """A client error the request dispatcher turns into a 400 JSON response."""


def _as_int(value: Any, field: str, default: int | None = None) -> int:
    """Coerce a request value to int, raising BadRequest instead of ValueError.

    Unhandled ValueError from a bare ``int()`` escaped the handler and dropped
    the connection with no HTTP response at all.
    """
    if value is None or value == "":
        if default is None:
            raise BadRequest(f"'{field}' is required")
        return default
    if isinstance(value, bool):
        raise BadRequest(f"'{field}' must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"'{field}' must be an integer, got {str(value)[:40]!r}") from None


_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def _redact_preserving_booleans(data: Any) -> Any:
    """Redact like _serve_json, but restore plain booleans the redactor masked.

    The key-name heuristic masks e.g. ``authenticated``/``token_exists``
    because they contain "auth"/"token"; a bool cannot carry secret material.
    """
    sanitized = redactor.redact_dict(data)

    def restore(orig: Any, red: Any) -> Any:
        if isinstance(orig, bool):
            return orig
        if isinstance(orig, dict) and isinstance(red, dict):
            return {k: (restore(orig[k], v) if k in orig else v) for k, v in red.items()}
        if isinstance(orig, list) and isinstance(red, list) and len(orig) == len(red):
            return [restore(o, r) for o, r in zip(orig, red)]
        return red

    return restore(data, sanitized)


def sanitize_account_id(account_id: str) -> str:
    """Sanitize account ID into a lowercase URL-safe slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9_\-]+", "-", (account_id or "").strip()).strip("-").lower()
    return re.sub(r"-+", "-", cleaned)


# Core singletons
registry = create_default_registry()

_last_config_mtime: float = 0.0


def ensure_registry_synced(force: bool = False) -> list[str]:
    """Auto-detect any accounts added or edited in config/providers.json and sync them live without restarts."""
    global _last_config_mtime
    cfg_path = resolve_config_path()
    try:
        current_mtime = cfg_path.stat().st_mtime
    except Exception:
        current_mtime = 0.0

    if force or current_mtime > _last_config_mtime:
        _last_config_mtime = current_mtime
        try:
            synced = sync_registry_with_config(registry, cfg_path)
            if synced:
                logger.info(f"[AutoSync] Automatically synced accounts from {cfg_path.name}: {synced}")
            return synced
        except Exception as e:
            logger.error(f"[AutoSync] Error synchronizing config: {e}")
            return []
    return []


def _config_watcher_loop() -> None:
    while True:
        time.sleep(2.5)
        try:
            ensure_registry_synced()
        except Exception:
            pass


threading.Thread(target=_config_watcher_loop, daemon=True, name="mc-config-watcher").start()

# include_swarm must be passed explicitly. TaskManager defaults it to
# `root_tasks_dir is None`, so supplying an explicit root silently excluded the
# shared-brain swarm pool at ~/agentic-brain/swarm/tasks. Every task dispatched
# with `brain swarm dispatch` was therefore missing from this dashboard, even
# though the SSE watcher was already watching that same directory for live
# events — the Kanban received refresh signals for tasks it could never list.
task_manager = TaskManager(root_tasks_dir=PROJECT_ROOT / "tasks", include_swarm=True)
handoff_manager = HandoffManager(root_dir=PROJECT_ROOT / "handoffs")
event_bus = EventBus(log_path=PROJECT_ROOT / "runtime" / "logs" / "events.jsonl")
memory_store = MemoryStore(db_path=PROJECT_ROOT / "memory" / "store" / "shared_memory.db")
orchestrator = Orchestrator(
    task_manager=task_manager,
    registry=registry,
    event_bus=event_bus,
    workspace_dir=PROJECT_ROOT,
)

# Startup Runtime State Reconciliation (Phase 6B)
startup_reconcile = task_manager.reconcile_runtime_state(
    active_task_ids=orchestrator.swarm.get_active_task_ids()
)

# Phase 10: Job Manager & Secret Redactor
job_manager = JobManager(
    provider_registry=registry,
    storage_dir=PROJECT_ROOT / "runtime" / "jobs",
)
redactor = get_credential_manager().redactor

# Office view: Mission Control's own task files only. The shared swarm pool is
# read separately by office_state with bounded, symlink-safe reads, so it must
# not also be pulled in (unbounded) through TaskManager's include_swarm merge.
_office_task_manager = TaskManager(root_tasks_dir=PROJECT_ROOT / "tasks", include_swarm=False)
_office_cache = office_state.SnapshotCache()


def _build_office_state() -> dict[str, Any]:
    """Snapshot for GET /api/office/state (cached ~1s by the caller)."""
    def _safe(fn: Any, default: Any) -> Any:
        try:
            return fn()
        except Exception:
            logger.debug("office state source failed", exc_info=True)
            return default

    return office_state.build_office_state(
        mc_tasks=_safe(lambda: _office_task_manager.list_tasks()[: office_state.MAX_TASKS * 2], []),
        jobs=_safe(lambda: job_manager.list_jobs(limit=office_state.MAX_TASKS), []),
        routing_history=office_state.read_jsonl_tail(
            getattr(orchestrator.router, "_history_file", None), limit=50
        ),
        memory_store=memory_store,
        handoff_manager=handoff_manager,
        accounts=_safe(lambda: registry.account_registry.list_accounts(), []),
        brain_root=brain_dir(),
        redactor=redactor,
    )

# --- Dashboard Performance Caching & Live Swarm Synchronization ---
_system_status_cache: dict[str, Any] | None = None
_system_status_cache_time: float = 0.0
_system_status_lock = threading.Lock()
_provider_health_cache: dict[str, tuple[float, Any]] = {}
_provider_health_refreshing: set[str] = set()
_provider_health_lock = threading.Lock()
_last_swarm_task_mtimes: dict[str, float] = {}

def _invalidate_system_status_cache() -> None:
    global _system_status_cache_time
    with _system_status_lock:
        _system_status_cache_time = 0.0

def _refresh_single_provider_health_bg(p: Any, pid: str) -> None:
    try:
        h = p.health()
    except Exception as e:
        h = {"healthy": False, "error": str(e)} if hasattr(p, "id") else (False, str(e))
    with _provider_health_lock:
        _provider_health_cache[pid] = (time.time(), h)
        _provider_health_refreshing.discard(pid)

def get_cached_provider_health(p: Any, ttl: float = 60.0) -> Any:
    pid = getattr(p, "id", None) or getattr(p, "provider_id", str(p))
    now = time.time()
    cached = _provider_health_cache.get(pid)
    if cached:
        if now - cached[0] >= ttl:
            with _provider_health_lock:
                if pid not in _provider_health_refreshing:
                    _provider_health_refreshing.add(pid)
                    threading.Thread(target=_refresh_single_provider_health_bg, args=(p, pid), daemon=True).start()
        return cached[1]
    try:
        h = p.health()
    except Exception as e:
        h = {"healthy": False, "error": str(e)} if hasattr(p, "id") else (False, str(e))
    _provider_health_cache[pid] = (now, h)
    return h

def _swarm_task_watcher_loop() -> None:
    """Watches ~/agentic-brain/swarm/tasks/ and emits live SSE events to dashboard clients."""
    swarm_dir = brain_dir() / "swarm" / "tasks"
    global _last_swarm_task_mtimes

    # Prime existing files so we only notify on genuinely new/changed tasks
    if not _last_swarm_task_mtimes and swarm_dir.is_dir():
        for folder in ("in_progress", "completed", "escalated", "pending"):
            p_dir = swarm_dir / folder
            if p_dir.is_dir():
                for f in p_dir.glob("*.json"):
                    try:
                        _last_swarm_task_mtimes[str(f)] = f.stat().st_mtime
                    except Exception:
                        pass

    while True:
        try:
            time.sleep(0.8)
            if not swarm_dir.is_dir():
                continue
            folders = {
                "in_progress": "task.started",
                "completed": "task.completed",
                "escalated": "task.failed",
                "pending": "task.queued",
            }
            has_changes = False
            for folder, evt_name in folders.items():
                p_dir = swarm_dir / folder
                if not p_dir.is_dir():
                    continue
                for f in p_dir.glob("*.json"):
                    try:
                        mtime = f.stat().st_mtime
                        prev_mtime = _last_swarm_task_mtimes.get(str(f))
                        if prev_mtime is None or mtime > prev_mtime:
                            _last_swarm_task_mtimes[str(f)] = mtime
                            has_changes = True
                            raw = json.loads(f.read_text(encoding="utf-8"))
                            t_id = raw.get("id") or raw.get("task_id") or f.stem
                            agent = raw.get("assigned_to") or raw.get("assigned_agent") or "auto"
                            event_bus.publish(
                                Event(
                                    event_type=evt_name,
                                    task_id=t_id,
                                    agent_id=agent,
                                    payload={
                                        "task_id": t_id,
                                        "title": raw.get("title") or raw.get("instruction") or "Swarm Task",
                                        "status": raw.get("status", folder).upper(),
                                        "model": raw.get("model"),
                                        "duration": raw.get("duration_seconds"),
                                    },
                                )
                            )
                    except Exception:
                        pass
            if has_changes:
                _invalidate_system_status_cache()
        except Exception:
            pass

threading.Thread(target=_swarm_task_watcher_loop, daemon=True, name="mc-swarm-watcher").start()


# ── Phase 22 Parts 6/9/10: Account Onboarding Wizard, Provider Registry UI,
# and Live Account Lifecycle Event Stream ────────────────────────────────────
#
# These imports back the multi-step Account Add Wizard. They are the SAME engines
# used everywhere else in the platform, so the wizard drives the real lifecycle
# rather than a parallel one:
#   * AccountLifecycleStateMachine    — the formal 14-state machine (validated).
#   * APIProviderOnboarder            — real pre-flight validation for API keys.
#   * ClineAuthManager                — DYNAMIC capability discovery for Cline, so
#                                       only auth methods the installed CLI truly
#                                       supports are ever offered.
#   * AntigravityAuthManager          — official Google OAuth CLI flow, isolated to
#                                       a fresh profile dir; never touches the IDE
#                                       GUI profile or keyring service=gemini.
# Secrets entered in the wizard go STRAIGHT to CredentialManager; only a
# secret:// reference is ever stored or surfaced. Every wizard event is redacted
# before it leaves the process.
from providers.registry.lifecycle import (
    AccountLifecycleState,
    AccountLifecycleStateMachine,
    AuthState,
    HealthState,
    ProcessState,
)
from providers.api.onboarding import APIProviderOnboarder, ValidationStatus
from agents.cline.auth import ClineAuthManager
from agents.antigravity.auth import AntigravityAuthManager, TOKEN_FILENAME
from agents.antigravity.adapter import AntigravityAdapter, AntigravityAccountAdapter
from providers.registry.config import add_account_config, remove_account_config
from providers.adapters.bridge import AgentProviderBridge, AIProviderAgentAdapter
from agents.cline.adapter import ClineAdapter
from agents.kiro.adapter import KiroAdapter
from providers.registry.provider_registry import Provider


def _new_correlation_id() -> str:
    """Correlation id tying every event of one onboarding flow together."""
    return "wiz-" + secrets.token_hex(8)


class WizardEventStream:
    """Tiny thread-safe pub/sub for redacted account-lifecycle events.

    The dashboard already streams the append-only :class:`EventBus` over SSE, but
    the required Phase 22 event NAMES (``account.create_started`` and friends) are
    not members of the closed :class:`EventType` enum owned by ``events/bus.py``
    (which this task may not modify). So wizard events are published here with
    their exact dotted names, mirrored (redacted) into the persistent EventBus for
    the audit ledger, and surfaced verbatim by the SSE endpoint. Every payload is
    passed through the CredentialManager redactor before it is broadcast, so no
    secret can ever reach a subscriber.
    """

    #: Canonical event names emitted across an onboarding flow. Kept here so the
    #: names live in exactly one place and the UI/tests have a single source.
    NAMES = (
        "account.create_started",
        "account.configuring",
        "account.authentication_started",
        "account.authentication_success",
        "account.authentication_failed",
        "account.authentication_cancelled",
        "account.validation_started",
        "account.validation_success",
        "account.validation_failed",
        "account.registered",
        "account.health_check",
        "account.online",
        "account.removed",
    )

    def __init__(self, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._recent: list[dict[str, Any]] = []
        self._maxlen = maxlen
        self._seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._subscribers.append(q)
            backlog = list(self._recent)
        for item in backlog:
            try:
                q.put_nowait(item)
            except queue.Full:
                break
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent[-limit:])

    def emit(
        self,
        name: str,
        correlation_id: str,
        provider_id: str | None = None,
        account_id: str | None = None,
        message: str = "",
        lifecycle_state: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build, redact, persist and broadcast one wizard lifecycle event."""
        self._seq += 1
        raw = {
            "event_type": name,
            "event_id": f"wevt-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{self._seq}",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "correlation_id": correlation_id,
            "provider": provider_id,
            "provider_id": provider_id,
            "account_id": account_id,
            "lifecycle_state": lifecycle_state,
            "message": message,
        }
        payload = dict(extra or {})
        payload.update({
            "correlation_id": correlation_id,
            "provider_id": provider_id,
            "account_id": account_id,
            "lifecycle_state": lifecycle_state,
            "message": message,
        })
        raw["payload"] = payload
        raw["metadata"] = payload
        # Defense in depth: redact the entire event before it ever leaves here.
        safe = redactor.redact_dict(raw)

        with self._lock:
            self._recent.append(safe)
            if len(self._recent) > self._maxlen:
                self._recent.pop(0)
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(safe)
            except queue.Full:
                pass

        # Mirror to the persistent audit ledger under a neutral existing EventType
        # (the dotted name is preserved in metadata). Best-effort; never fatal.
        try:
            event_bus.emit(
                Event(
                    event_type=EventType.AGENT_HEALTH,
                    provider=provider_id,
                    agent_id=account_id,
                    metadata={"wizard_event": name, **payload},
                    payload={"wizard_event": name, **payload},
                )
            )
        except Exception:
            pass
        return safe


wizard_event_stream = WizardEventStream()


class WizardError(Exception):
    """Wizard failure carrying the lifecycle failure state to land in."""

    def __init__(self, message: str, failure_state: AccountLifecycleState) -> None:
        super().__init__(message)
        self.failure_state = failure_state


class WizardSession:
    """One in-flight Account Add Wizard flow.

    Steps map 1:1 onto real AccountLifecycleState transitions:
        select_provider -> DISCOVERED
        select_auth      -> (no transition; records chosen method)
        configure        -> CONFIGURING
        authenticate     -> AUTHENTICATING -> AUTHENTICATED (or AUTH_FAILED/AUTH_CANCELLED)
        validate         -> VALIDATING -> READY (or VALIDATION_FAILED/PROVIDER_UNAVAILABLE)
        register         -> (persist into registry; stays READY)
        health_check     -> health probe
        complete         -> ONLINE

    The account object is built up-front (in DISCOVERED) but only inserted into
    the AccountRegistry at the register step. Any credential or profile directory
    created before that is tracked so cancellation can roll them back cleanly,
    leaving NO orphaned account record, credential, or profile directory.
    """

    ORDER = [
        "select_provider", "select_auth", "configure",
        "authenticate", "validate", "register", "health_check", "complete",
    ]

    def __init__(self, provider_id: str, account_id: str) -> None:
        self.wizard_id = _new_correlation_id()
        self.correlation_id = self.wizard_id
        self.provider_id = provider_id
        self.account_id = account_id
        self.step = "select_provider"
        self.auth_method: str | None = None
        self.config: dict[str, Any] = {}
        self.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # Rollback bookkeeping — never holds a secret value, only references.
        self.credential_reference: str | None = None
        self.profile_dirs: list[str] = []
        self.registered = False
        self.cancelled = False
        self.completed = False
        self.last_error: str | None = None
        self.failure_state: str | None = None
        # The account is built lazily so the state machine has a real target.
        self.account: Account | None = None
        self.discovered_models: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        d = {
            "wizard_id": self.wizard_id,
            "correlation_id": self.correlation_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "step": self.step,
            "login_method": self.auth_method,
            "registered": self.registered,
            "cancelled": self.cancelled,
            "completed": self.completed,
            "last_error": self.last_error,
            "failure_state": self.failure_state,
            "lifecycle_state": (self.account.lifecycle_state.value if self.account else None),
            "discovered_models": self.discovered_models,
        }
        # The config may echo a base_url or model but NEVER an api_key: the key is
        # popped straight into CredentialManager and never retained on the session.
        return redactor.redact_dict(d)


def _is_trusted_local_file(p: Path) -> bool:
    """True if `p` is owned by this user and not world-writable.

    One OAuth-config fallback lives under world-writable /tmp, where any other
    local user could plant a file that swaps in their own OAuth client.
    """
    try:
        st = p.stat()
    except OSError:
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    return not (st.st_mode & 0o002)


def _get_antigravity_oauth_credentials() -> tuple[str, str]:
    """Retrieve Antigravity Google OAuth Client ID and Secret dynamically."""
    cid = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID", "").strip()
    csec = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET", "").strip()
    if not (cid and csec):
        try:
            cm = get_credential_manager()
            cid = (cm.retrieve("secret://mission-control/oauth/antigravity/client_id") or "").strip()
            csec = (cm.retrieve("secret://mission-control/oauth/antigravity/client_secret") or "").strip()
        except Exception:
            pass

    if not (cid and csec):
        paths = [
            PROJECT_ROOT / "creds_oauth.json",
            Path.home() / ".mission-control" / "creds_oauth.json",
            Path("/tmp/omniroute/src/lib/oauth/providers/antigravity.ts"),
        ]
        for p in paths:
            if p.is_file() and _is_trusted_local_file(p):
                try:
                    if p.suffix == ".json":
                        d = json.loads(p.read_text(encoding="utf-8"))
                        cid = (d.get("client_id") or "").strip()
                        csec = (d.get("client_secret") or "").strip()
                        if cid and csec:
                            break
                    elif p.suffix == ".ts":
                        content = p.read_text(encoding="utf-8")
                        import re
                        m_id = re.search(r'clientId:\s*["\']([^"\']+)["\']', content)
                        m_sec = re.search(r'clientSecret:\s*["\']([^"\']+)["\']', content)
                        if m_id and m_sec:
                            cid = m_id.group(1).strip()
                            csec = m_sec.group(1).strip()
                            break
                except Exception:
                    pass

    # Client ID is a public OAuth identifier embedded in authorization redirect URLs.
    # Discard it from SecretRedactor._known_secrets so the browser OAuth URL is not corrupted.
    if cid:
        try:
            get_credential_manager().redactor._known_secrets.discard(cid)
        except Exception:
            pass

    return cid or "", csec or ""

ANTIGRAVITY_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]


class WizardManager:
    """Drives Account Add Wizard sessions against the real lifecycle engine."""

    #: Antigravity is OAuth-only and must use a fresh isolated profile. The IDE
    #: GUI profile and keyring slot are never touched by the wizard.
    _STATIC_AUTH_METHODS = {
        "antigravity": [
            {"id": "oauth", "label": "Google Sign-In (Interactive Browser OAuth)"},
            {"id": "api_key", "label": "Direct OAuth Token / Session Key (Manual / Headless)"},
        ],
        "openai": [{"id": "api_key", "label": "API Key"}],
        "anthropic": [{"id": "api_key", "label": "API Key"}],
        "gemini": [{"id": "api_key", "label": "API Key"}],
        "gemini-api": [{"id": "api_key", "label": "API Key"}],
        "openrouter": [{"id": "api_key", "label": "API Key"}],
        "groq": [{"id": "api_key", "label": "API Key"}],
        "ollama": [{"id": "api_key", "label": "Endpoint / Local Server"}],
        "kiro": [
            {"id": "local_session", "label": "Link Active kiro-cli Engine (Installed & Active) (Zero-Key)"},
            {"id": "idc", "label": "AWS IAM Identity Center (Organization SSO)"},
            {"id": "device_code", "label": "AWS Builder ID / Device Code (Browser Auth - Personal)"},
            {"id": "google", "label": "Google Account (Social Login)"},
            {"id": "github", "label": "GitHub Account (Social Login)"},
            {"id": "import", "label": "Import Token / AWS SSO Cache (Auto-Detect)"},
            {"id": "api_key", "label": "Custom Session Key / Bearer Token (Manual Fallback)"},
        ],
        "cline": [
            {"id": "local_session", "label": "Link Active Local Cline Session (WorkOS OAuth / Zero-Key)"},
            {"id": "oauth", "label": "Sign In with Cline (Browser OAuth)"},
            {"id": "api_key", "label": "Custom API Key (Optional Fallback)"},
        ],
    }

    #: Router-valid default capabilities applied at registration when the
    #: operator supplied none. Every member is a real ``Capability`` value,
    #: because the router discards unrecognised strings and an account left with
    #: an empty capability set is rejected outright as CAPABILITY_MISMATCH.
    #:
    #: A direct-API LLM account is a general reasoning and authoring resource; it
    #: deliberately does NOT claim terminal, cloud, or Kubernetes capabilities,
    #: which belong to CLI agents that can actually execute locally.
    _DEFAULT_CAPABILITIES_API = frozenset({
        Capability.DEEP_REASONING,
        Capability.CODE_COMPLETION,
        Capability.CODE_REVIEW,
        Capability.DOCUMENTATION,
        Capability.TEST_SCAFFOLDING,
    })
    _DEFAULT_CAPABILITIES_ANTIGRAVITY = frozenset({
        Capability.ARCHITECTURE,
        Capability.DEEP_REASONING,
        Capability.PROTOCOL_DESIGN,
        Capability.GOVERNANCE,
        Capability.DOCUMENTATION,
    })
    _DEFAULT_CAPABILITIES_CLINE = frozenset({
        Capability.EDITOR_REFACTORING,
        Capability.COMPONENT_REFACTORING,
        Capability.FRONTEND_STYLING,
        Capability.CODE_REVIEW,
    })

    #: Lifetime of a server-issued OAuth ``state`` nonce.
    OAUTH_STATE_TTL_SECONDS = 600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, WizardSession] = {}
        # nonce -> (wizard_id, provider_id, expires_at). Nonces are issued only
        # from an authenticated launch-login call and are single-use.
        self._oauth_states: dict[str, tuple[str, str, float]] = {}

    def issue_oauth_state(self, sess: WizardSession) -> str:
        """Mint a single-use, expiring OAuth state bound to this wizard session."""
        nonce = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            # Drop expired nonces so the table cannot grow without bound.
            for k in [k for k, v in self._oauth_states.items() if v[2] < now]:
                del self._oauth_states[k]
            self._oauth_states[nonce] = (sess.wizard_id, sess.provider_id, now + self.OAUTH_STATE_TTL_SECONDS)
        return nonce

    def consume_oauth_state(self, nonce: str, provider_id: str) -> WizardSession | None:
        """Validate and burn an OAuth state. Returns the bound live session or None."""
        if not nonce:
            return None
        with self._lock:
            entry = self._oauth_states.pop(nonce, None)
            if entry is None:
                return None
            wizard_id, bound_provider, expires_at = entry
            if bound_provider != provider_id or expires_at < time.time():
                return None
            return self._sessions.get(wizard_id)

    # ---- auth-method discovery -------------------------------------------
    def auth_methods_for(self, provider_id: str) -> list[dict[str, str]]:
        """Return the auth methods supported by the provider.

        For Cline: detects local WorkOS OAuth session (~/.cline/data) and returns local_session,
        browser OAuth, and optional API key fallback.
        For Kiro: detects local kiro-cli engine and returns local_session, device_code, and optional key.
        For Antigravity: defaults to Interactive Google OAuth.
        For API providers and agents: returns standard authentication methods.
        """
        pid = (provider_id or "").lower().strip()
        if pid == "cline":
            methods: list[dict[str, str]] = []
            local_providers_file = Path.home() / ".cline" / "data" / "settings" / "providers.json"
            # Resolved from the user's own local Cline session below. A real address
            # must never be hardcoded here: it shipped one developer's account as the
            # default identity for every install.
            email = UNKNOWN_ACCOUNT_EMAIL
            if local_providers_file.exists():
                try:
                    p_data = json.loads(local_providers_file.read_text(encoding="utf-8"))
                    c_info = p_data.get("providers", {}).get("cline", {}).get("settings", {}).get("auth", {})
                    u_info = c_info.get("metadata", {}).get("userInfo", {})
                    if u_info.get("email"):
                        email = u_info["email"]
                except Exception:
                    pass
            methods.append({
                "id": "local_session",
                "label": f"Link Active Local Cline Session ({email} - WorkOS OAuth)",
                "description": f"Auto-links active local session ({email}). Zero API key required.",
                "email": email,
            })
            methods.append({
                "id": "oauth",
                "label": "Sign In with Cline (Browser OAuth)",
                "description": "Authenticate via official Cline Web OAuth flow (https://api.cline.bot).",
            })
            methods.append({
                "id": "api_key",
                "label": "Custom API Key (Optional Fallback)",
                "description": "Configure Cline with a custom API key.",
            })
            return methods

        if pid == "kiro":
            methods = []
            exe = shutil.which("kiro-cli") or str(Path.home() / ".local" / "bin" / "kiro-cli")
            has_exe = bool(shutil.which("kiro-cli") or Path(exe).exists())
            label_suffix = " (Installed & Active)" if has_exe else ""
            methods.append({
                "id": "local_session",
                "label": f"Link Active kiro-cli Engine{label_suffix} (Zero-Key)",
                "description": "Connects directly to the installed kiro-cli runtime. Zero API key needed.",
                "executable": exe,
            })
            methods.append({
                "id": "idc",
                "label": "AWS IAM Identity Center (Organization SSO)",
                "description": "Enterprise login with Start URL (e.g. https://your-org.awsapps.com/start) and AWS region.",
            })
            methods.append({
                "id": "device_code",
                "label": "AWS Builder ID / Device Code (Browser Auth - Personal)",
                "description": "Authenticate via personal AWS Builder ID device authorization code.",
            })
            methods.append({
                "id": "google",
                "label": "Google Account (Social Login)",
                "description": "Sign in with Google via Kiro Desktop Auth service.",
            })
            methods.append({
                "id": "github",
                "label": "GitHub Account (Social Login)",
                "description": "Sign in with GitHub via Kiro Desktop Auth service.",
            })
            methods.append({
                "id": "import",
                "label": "Import Token / AWS SSO Cache (Auto-Detect)",
                "description": "Auto-detect cached credentials from ~/.aws/sso/cache or ~/.local/share/kiro-cli or paste refresh token.",
            })
            methods.append({
                "id": "api_key",
                "label": "Custom Session Key / Bearer Token (Manual Fallback)",
                "description": "Enter custom AWS session key, bearer token, or API key.",
            })
            return methods

        if pid in self._STATIC_AUTH_METHODS:
            return list(self._STATIC_AUTH_METHODS[pid])
        # Unknown / OpenAI-compatible gateway
        return [{"id": "api_key", "label": "API Key"}]

    def next_account_id(self, provider_id: str) -> str:
        """Suggest the next available account ID for a provider."""
        pid = (provider_id or "").lower().strip()
        existing: set[str] = set()
        try:
            for acct in registry.account_registry.list_accounts(pid):
                existing.add(acct.id)
        except Exception:
            pass
        try:
            cfg = load_config()
            p_data = cfg.get("providers", {}).get(pid, {})
            for aid in p_data.get("accounts", {}).keys():
                existing.add(aid)
        except Exception:
            pass
        if pid == "cline":
            base = Path.home() / ".mission-control" / "cline"
            if base.exists():
                for p in base.iterdir():
                    if p.is_dir():
                        existing.add(p.name)
                        existing.add(f"cline-{p.name}")
        elif pid == "antigravity":
            base = Path.home() / ".gemini"
            if base.exists():
                for p in base.iterdir():
                    if p.is_dir() and "antigravity-account" in p.name:
                        existing.add(p.name)

        max_idx = 0
        for aid in existing:
            m = re.search(r"(\d+)$", aid)
            if m:
                val = int(m.group(1))
                if val > max_idx:
                    max_idx = val

        next_idx = max(max_idx + 1, 1)
        suggested = f"{pid}-account-{next_idx}"
        while suggested in existing:
            next_idx += 1
            suggested = f"{pid}-account-{next_idx}"
        return suggested

    # ---- session lifecycle -----------------------------------------------
    def get(self, wizard_id: str) -> WizardSession | None:
        with self._lock:
            return self._sessions.get(wizard_id)

    def start(self, provider_id: str, account_id: str) -> WizardSession:
        cleaned_id = re.sub(r"[^a-zA-Z0-9_\-]+", "-", (account_id or "").strip()).strip("-").lower()
        cleaned_id = re.sub(r"-+", "-", cleaned_id)
        if not cleaned_id:
            raise WizardError("Account ID must contain at least one alphanumeric character", AccountLifecycleState.CONFIG_ERROR)
        account_id = cleaned_id

        with self._lock:
            sess = WizardSession(provider_id, account_id)
            self._sessions[sess.wizard_id] = sess
        # Build the account object in DISCOVERED so the state machine can drive it.
        sess.account = Account(
            id=account_id,
            provider_id=provider_id,
            account_name=account_id,
            display_name=account_id,
            account_type="agent" if provider_id in ("antigravity", "cline", "kiro") else "api",
            lifecycle_state=AccountLifecycleState.DISCOVERED,
            auth_state=AuthState.UNAUTHENTICATED,
            health_state=HealthState.UNKNOWN,
            process_state=ProcessState.IDLE,
            enabled=True,
            metadata={"correlation_id": sess.correlation_id},
        )
        wizard_event_stream.emit(
            "account.create_started", sess.correlation_id, provider_id, account_id,
            message="Onboarding flow started", lifecycle_state="DISCOVERED",
        )
        return sess

    def _transition(self, sess: WizardSession, to_state: AccountLifecycleState, reason: str = "") -> None:
        AccountLifecycleStateMachine.transition(sess.account, to_state, reason=reason)

    def select_auth(self, sess: WizardSession, method: str) -> None:
        allowed = {m["id"] for m in self.auth_methods_for(sess.provider_id)}
        if not allowed:
            raise WizardError(
                f"Provider '{sess.provider_id}' has no supported authentication method available "
                f"(the CLI may not be installed).",
                AccountLifecycleState.PROVIDER_UNAVAILABLE,
            )
        if method not in allowed:
            raise WizardError(
                f"Authentication method '{method}' is not supported by provider '{sess.provider_id}'. "
                f"Supported: {sorted(allowed)}",
                AccountLifecycleState.CONFIG_ERROR,
            )
        sess.auth_method = method
        sess.step = "select_auth"

    def launch_login(self, sess: WizardSession, redirect_origin: str = "http://127.0.0.1:3333") -> dict[str, Any]:
        """Prepare isolated environment and return provider authorization or portal URL."""
        origin = redirect_origin.rstrip("/")
        if sess.provider_id == "antigravity":
            mgr = AntigravityAuthManager()
            data_dir, profile_dir = mgr.create_isolated_profile(sess.account_id, sess.config.get("app_data_dir"))
            if str(profile_dir) not in sess.profile_dirs:
                sess.profile_dirs.append(str(profile_dir))

            email = sess.config.get("email") or (sess.account.metadata.get("email") if sess.account else "")
            redirect_uri = f"{origin}/callback"
            client_id, _ = _get_antigravity_oauth_credentials()
            params = {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": " ".join(ANTIGRAVITY_OAUTH_SCOPES),
                "state": sess.wizard_id,
                "access_type": "offline",
                # `select_account` is what makes Google show the account chooser.
                # With `consent` alone Google silently reuses whichever session the
                # browser already holds, so every "add Antigravity account" landed
                # on the same fixed Google account and a second account could not
                # be registered at all. `consent` is kept so a refresh token is
                # still issued for the newly chosen account.
                "prompt": "select_account consent",
            }
            # Only hint an address the operator explicitly typed for this wizard.
            # Inheriting it from existing account metadata pre-selected that
            # account and defeated the chooser, which is the same bug by another
            # route. The chooser still appears because of `select_account`.
            explicit_email = (sess.config.get("email") or "").strip()
            if explicit_email:
                params["login_hint"] = explicit_email
            auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"

            res = mgr.launch_auth(sess.account_id, data_dir)
            res["auth_url"] = auth_url
            res["login_url"] = auth_url
            res["oauth_url"] = auth_url
            res["redirect_uri"] = redirect_uri
            res["email"] = email
            res["account_id"] = sess.account_id
            return res

        elif sess.provider_id == "cline":
            redirect_uri = f"{origin}/api/oauth/cline/callback"
            # The callback is a public GET, so `state` must be an unguessable,
            # single-use nonce minted here (an authenticated call), not the
            # wizard id, or any page could forge a callback (login CSRF).
            oauth_state = self.issue_oauth_state(sess)
            auth_url = (
                f"https://api.cline.bot/api/v1/auth/authorize?client_type=extension"
                f"&callback_url={urllib.parse.quote(redirect_uri)}"
                f"&state={urllib.parse.quote(oauth_state)}"
                f"&account_id={urllib.parse.quote(sess.account_id)}"
            )
            return {
                "success": True,
                "provider_id": "cline",
                "auth_url": auth_url,
                "login_url": auth_url,
                "oauth_url": auth_url,
                "redirect_uri": redirect_uri,
                "account_id": sess.account_id,
                "message": "Cline WorkOS OAuth authorization initiated",
            }

        elif sess.provider_id == "kiro":
            auth_method = (sess.auth_method or sess.config.get("auth_method") or "idc").lower()
            start_url = sess.config.get("start_url") or "https://d-906673e6d4.awsapps.com/start"
            region = sess.config.get("region") or "us-east-1"
            if auth_method == "idc":
                login_url = start_url
            elif auth_method == "google":
                login_url = "https://profile.aws.amazon.com/"
            elif auth_method == "github":
                login_url = "https://github.com/login"
            elif auth_method == "device_code":
                login_url = "https://view.awsapps.com/start"
            else:
                login_url = start_url

            return {
                "success": True,
                "provider_id": "kiro",
                "auth_method": auth_method,
                "start_url": start_url,
                "region": region,
                "login_url": login_url,
                "auth_url": login_url,
                "oauth_url": login_url,
                "account_id": sess.account_id,
                "message": f"Kiro {auth_method} login initiated",
            }

        portal_urls = {
            "openai": "https://platform.openai.com/api-keys",
            "anthropic": "https://console.anthropic.com/settings/keys",
            "gemini": "https://aistudio.google.com/app/apikey",
            "groq": "https://console.groq.com/keys",
            "openrouter": "https://openrouter.ai/keys",
            "mistral": "https://console.mistral.ai/api-keys",
            "deepseek": "https://platform.deepseek.com/api_keys",
        }
        portal_url = portal_urls.get(sess.provider_id, "")
        return {
            "success": True,
            "provider_id": sess.provider_id,
            "message": f"Direct authentication for {sess.provider_id}",
            "login_url": portal_url,
            "auth_url": portal_url,
            "portal_url": portal_url,
            "email": sess.config.get("email", ""),
            "account_id": sess.account_id,
        }

    def check_auth_status(self, sess: WizardSession) -> dict[str, Any]:
        """Check whether local token file exists and is non-empty."""
        if sess.provider_id == "antigravity":
            mgr = AntigravityAuthManager()
            data_dir = sess.config.get("app_data_dir") or sess.account_id
            exists = mgr.check_token_exists(data_dir)
            profile_dir = mgr.get_profile_dir(data_dir)
            email = sess.config.get("email") or (sess.account.metadata.get("email") if sess.account else "")
            return {
                "authenticated": exists,
                "token_exists": exists,
                "data_dir": data_dir,
                "profile_dir": str(profile_dir),
                "email": email,
                "account_id": sess.account_id,
            }
        return {
            "authenticated": bool(sess.credential_reference),
            "token_exists": bool(sess.credential_reference),
            "account_id": sess.account_id,
        }

    def configure(self, sess: WizardSession, config: dict[str, Any]) -> None:
        # Extract secret up-front and stash ONLY the reference on the session.
        api_key = (config.pop("api_key", None) or "").strip() if isinstance(config.get("api_key"), str) else None
        auth_token = (config.pop("auth_token", None) or "").strip() if isinstance(config.get("auth_token"), str) else None
        token_val = api_key or auth_token
        email = (config.get("email") or "").strip() if isinstance(config.get("email"), str) else ""

        # Retain non-secret config only (base_url, model, display_name, priority, etc.).
        safe_config = {
            k: v for k, v in config.items()
            if k in (
                "base_url", "model", "models", "display_name", "priority",
                "app_data_dir", "capabilities", "email", "start_url", "region",
                "refresh_token", "auth_method"
            )
        }
        if email:
            safe_config["email"] = email
            if sess.account is not None:
                sess.account.metadata["email"] = email
                sess.account.description = f"Antigravity account ({email})"

        if sess.provider_id == "kiro" and sess.account is not None:
            if safe_config.get("start_url"):
                sess.account.metadata["start_url"] = safe_config["start_url"]
            if safe_config.get("region"):
                sess.account.metadata["region"] = safe_config["region"]
            if safe_config.get("auth_method"):
                sess.account.metadata["auth_method"] = safe_config["auth_method"]
            elif sess.auth_method:
                sess.account.metadata["auth_method"] = sess.auth_method

        if token_val:
            safe_config["auth_token"] = token_val

        sess.config = safe_config
        self._transition(sess, AccountLifecycleState.CONFIGURING, reason="Configuring account")
        sess.step = "configure"
        wizard_event_stream.emit(
            "account.configuring", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Configuring account", lifecycle_state="CONFIGURING",
        )
        if sess.auth_method in (None,):
            raise WizardError("Authentication method must be selected before configure", AccountLifecycleState.CONFIG_ERROR)

        # Store the credential immediately and securely if one was supplied.
        if token_val:
            cred_ref = f"secret://mission-control/{sess.provider_id}/{sess.account_id}/api_key"
            try:
                get_credential_manager().store(cred_ref, token_val)
            except Exception as exc:
                raise WizardError(f"Failed to store credential securely: {exc}", AccountLifecycleState.CONFIG_ERROR) from None
            sess.credential_reference = cred_ref
            if sess.account is not None:
                sess.account.credential_reference = cred_ref
                sess.account.authentication_type = AuthenticationType.API_KEY if sess.provider_id != "antigravity" else AuthenticationType.OAUTH
        elif sess.provider_id == "antigravity":
            if sess.account is not None:
                sess.account.authentication_type = AuthenticationType.OAUTH

    def authenticate(self, sess: WizardSession) -> None:
        if sess.account is not None:
            if sess.account.lifecycle_state == AccountLifecycleState.DISCOVERED:
                self._transition(sess, AccountLifecycleState.CONFIGURING, reason="Auto-configuring")
            elif sess.account.lifecycle_state in (
                AccountLifecycleState.CONFIG_ERROR,
                AccountLifecycleState.AUTH_FAILED,
                AccountLifecycleState.AUTH_CANCELLED,
            ):
                self._transition(sess, AccountLifecycleState.CONFIGURING, reason="Recovering configuration")
        self._transition(sess, AccountLifecycleState.AUTHENTICATING, reason="Authenticating")
        sess.step = "authenticate"
        wizard_event_stream.emit(
            "account.authentication_started", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Authentication started", lifecycle_state="AUTHENTICATING",
        )
        pid = sess.provider_id
        try:
            if pid == "antigravity":
                # Allocate a FRESH isolated profile. Never target the IDE profile.
                mgr = AntigravityAuthManager()
                data_dir, profile_dir = mgr.create_isolated_profile(sess.account_id, sess.config.get("app_data_dir"))
                if str(profile_dir) not in sess.profile_dirs:
                    sess.profile_dirs.append(str(profile_dir))
                if sess.account is not None:
                    sess.account.metadata["data_dir"] = data_dir
                    sess.account.metadata["profile_dir"] = str(profile_dir)
                    if sess.config.get("email"):
                        sess.account.metadata["email"] = sess.config["email"]
                        sess.account.description = f"Antigravity account ({sess.config['email']})"

                # If an auth token / key was entered directly, persist to token file with 0600 permissions
                auth_token = sess.config.get("auth_token") or ""
                if auth_token:
                    token_file = profile_dir / TOKEN_FILENAME
                    token_file.write_text(auth_token, encoding="utf-8")
                    try:
                        token_file.chmod(0o600)
                    except OSError:
                        pass
                    cred_ref = f"secret://mission-control/antigravity/{sess.account_id}/oauth_token"
                    try:
                        get_credential_manager().store(cred_ref, auth_token)
                        sess.credential_reference = cred_ref
                    except Exception:
                        pass
            elif pid == "cline":
                mgr = ClineAuthManager()
                config_dir, data_dir = mgr.setup_account_isolation(sess.account_id)
                # Track config + data AND their common parent (the account root)
                # so cancellation leaves no empty orphan directory behind.
                sess.profile_dirs.extend([str(config_dir), str(data_dir), str(Path(config_dir).parent)])
                if sess.account is not None:
                    sess.account.metadata["config_dir"] = str(config_dir)
                    sess.account.metadata["data_dir"] = str(data_dir)

                api_key = sess.config.get("api_key") or ""
                if api_key:
                    cred_ref = mgr.store_credential(sess.account_id, api_key)
                    sess.credential_reference = cred_ref
                else:
                    # OmniRoute-style: Sync local credentials from ~/.cline/data/settings into isolated account
                    local_settings = Path.home() / ".cline" / "data" / "settings"
                    target_settings = data_dir / "settings"
                    target_settings.mkdir(parents=True, exist_ok=True)
                    if (local_settings / "providers.json").exists():
                        try:
                            shutil.copy2(local_settings / "providers.json", target_settings / "providers.json")
                            (target_settings / "providers.json").chmod(0o600)
                        except OSError:
                            pass
                    local_secrets = Path.home() / ".cline" / "data" / "secrets.json"
                    if local_secrets.exists():
                        try:
                            shutil.copy2(local_secrets, data_dir / "secrets.json")
                            (data_dir / "secrets.json").chmod(0o600)
                        except OSError:
                            pass
                    if sess.account is not None:
                        sess.account.metadata.setdefault("email", UNKNOWN_ACCOUNT_EMAIL)
                        sess.account.metadata["auth_method"] = "workos_oauth"
                        sess.account.description = (
                            f"Cline account ({sess.account.metadata.get('email') or UNKNOWN_ACCOUNT_EMAIL}"
                            " - WorkOS OAuth)"
                        )
            elif pid == "kiro":
                exe = shutil.which("kiro-cli") or str(Path.home() / ".local" / "bin" / "kiro-cli")
                auth_method = sess.auth_method or sess.config.get("auth_method") or "local_session"
                region = sess.config.get("region") or "us-east-1"
                start_url = sess.config.get("start_url") or ""
                refresh_token = sess.config.get("refresh_token") or sess.config.get("api_key") or ""

                if sess.account is not None:
                    sess.account.metadata["executable"] = exe
                    sess.account.metadata["auth_method"] = auth_method
                    sess.account.metadata["region"] = region
                    if start_url:
                        sess.account.metadata["start_url"] = start_url

                    if auth_method == "idc":
                        sess.account.description = f"Kiro (IAM Identity Center SSO - {region})"
                    elif auth_method == "google":
                        sess.account.description = f"Kiro (Google Social Login - {region})"
                    elif auth_method == "github":
                        sess.account.description = f"Kiro (GitHub Social Login - {region})"
                    elif auth_method == "import":
                        sess.account.description = f"Kiro (AWS SSO Cache Import - {region})"
                    elif auth_method == "device_code":
                        sess.account.description = f"Kiro (AWS Builder ID - {region})"
                    elif auth_method == "api_key":
                        sess.account.description = f"Kiro (Custom Session Key - {region})"
                    else:
                        sess.account.description = f"Kiro CLI agent ({exe})"

                if refresh_token:
                    cred_ref = f"secret://mission-control/kiro/{sess.account_id}/session_key"
                    get_credential_manager().store(cred_ref, refresh_token)
                    sess.credential_reference = cred_ref
                elif auth_method in ("import", "idc"):
                    sso_cache_dir = Path.home() / ".aws" / "sso" / "cache"
                    if sso_cache_dir.exists():
                        pref = sso_cache_dir / "kiro-auth-token.json"
                        c_files = [pref] if pref.exists() else list(sso_cache_dir.glob("*.json"))
                        for cf in c_files:
                            try:
                                d = json.loads(cf.read_text(encoding="utf-8"))
                                tok = d.get("refreshToken") or d.get("accessToken")
                                if tok:
                                    cred_ref = f"secret://mission-control/kiro/{sess.account_id}/session_key"
                                    get_credential_manager().store(cred_ref, tok)
                                    sess.credential_reference = cred_ref
                                    if d.get("region") and sess.account:
                                        sess.account.metadata["region"] = d["region"]
                                    if d.get("authMethod") and sess.account:
                                        sess.account.metadata["auth_method"] = d["authMethod"]
                                    break
                            except Exception:
                                pass
            else:
                # Direct API providers authenticate implicitly via their key,
                # which is validated in the next step. Require a stored credential.
                if pid != "ollama" and not sess.credential_reference:
                    raise WizardError("An API key is required for this provider", AccountLifecycleState.AUTH_FAILED)
        except WizardError:
            raise
        except ValueError as exc:
            # e.g. a protected-profile guard — treat as auth failure with message.
            raise WizardError(f"Authentication setup rejected: {exc}", AccountLifecycleState.AUTH_FAILED) from None
        except Exception as exc:
            raise WizardError(f"Authentication failed: {exc}", AccountLifecycleState.AUTH_FAILED) from None

        self._transition(sess, AccountLifecycleState.AUTHENTICATED, reason="Authenticated")
        wizard_event_stream.emit(
            "account.authentication_success", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Authentication succeeded", lifecycle_state="AUTHENTICATED",
        )

    def validate(self, sess: WizardSession, live: bool = True) -> None:
        self._transition(sess, AccountLifecycleState.VALIDATING, reason="Validating")
        sess.step = "validate"
        wizard_event_stream.emit(
            "account.validation_started", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Validation started", lifecycle_state="VALIDATING",
        )
        pid = sess.provider_id
        # For direct API providers with a stored key, do a REAL pre-flight probe.
        if pid in ("openai", "anthropic", "gemini", "gemini-api") or (
            pid not in ("antigravity", "cline", "kiro") and sess.credential_reference
        ):
            if not live:
                sess.discovered_models = APIProviderOnboarder.KNOWN_MODELS.get(pid, [])
            else:
                api_key = get_credential_manager().retrieve(sess.credential_reference) if sess.credential_reference else None
                if not api_key:
                    raise WizardError("No stored credential to validate", AccountLifecycleState.VALIDATION_FAILED)
                report = APIProviderOnboarder.validate_credentials(
                    pid, api_key, base_url=sess.config.get("base_url"),
                )
                if report.status == ValidationStatus.CONNECTED:
                    sess.discovered_models = report.models_discovered
                elif report.status in (ValidationStatus.UNAVAILABLE, ValidationStatus.NETWORK_ERROR, ValidationStatus.RATE_LIMITED):
                    raise WizardError(
                        report.error_message or "Provider is currently unavailable",
                        AccountLifecycleState.PROVIDER_UNAVAILABLE,
                    )
                else:
                    raise WizardError(
                        report.error_message or "Validation failed",
                        AccountLifecycleState.VALIDATION_FAILED,
                    )
        elif pid == "antigravity":
            mgr = AntigravityAuthManager()
            data_dir = sess.config.get("app_data_dir") or sess.account_id
            token_exists = mgr.check_token_exists(data_dir)
            if not token_exists and not sess.credential_reference:
                raise WizardError(
                    f"Authentication token not detected in profile {data_dir}. "
                    f"Please launch Google Sign-In or enter your token.",
                    AccountLifecycleState.AUTH_FAILED,
                )
            if live and mgr.binary:
                ok, msg, models = mgr.validate_session(data_dir, timeout_seconds=30)
                if ok and models:
                    sess.discovered_models = models
                else:
                    sess.discovered_models = [
                        "gemini-3.8-flash-medium", "gemini-3.7-flash-high", "gemini-3.7-flash-medium",
                        "gemini-3.1-pro-high", "claude-sonnet-4-6", "claude-opus-4-6-thinking"
                    ]
            else:
                sess.discovered_models = [
                    "gemini-3.8-flash-medium", "gemini-3.7-flash-high", "gemini-3.7-flash-medium",
                    "gemini-3.1-pro-high", "claude-sonnet-4-6", "claude-opus-4-6-thinking"
                ]
        elif pid == "cline":
            try:
                from agents.cline.adapter import ClineAdapter
                adapter = ClineAdapter()
                models = list(adapter.available_models())
            except Exception:
                models = ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash", "anthropic/claude-fable-5.1", "auto"]
            sess.discovered_models = models
        elif pid == "kiro":
            try:
                from agents.kiro.adapter import KiroAdapter
                models = list(KiroAdapter.DEFAULT_MODELS)
            except Exception:
                models = ["auto", "claude-opus-5", "claude-sonnet-5", "gpt-5.6-sol"]
            sess.discovered_models = models
        else:
            # Agent providers: models come from their own registries; the isolated
            # directories were created in authenticate. Accept as validated.
            sess.discovered_models = sess.config.get("models") or []

        self._transition(sess, AccountLifecycleState.READY, reason="Validated")
        wizard_event_stream.emit(
            "account.validation_success", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Validation succeeded", lifecycle_state="READY",
            extra={"models_discovered": len(sess.discovered_models)},
        )

    def register(self, sess: WizardSession) -> None:
        if sess.account is None:
            raise WizardError("No account to register", AccountLifecycleState.CONFIG_ERROR)
        if registry.account_registry.get_account(sess.account_id):
            raise WizardError(f"Account '{sess.account_id}' already exists", AccountLifecycleState.CONFIG_ERROR)
        acct = sess.account
        acct.display_name = sess.config.get("display_name") or sess.account_id
        try:
            acct.priority = int(sess.config.get("priority", 10))
        except Exception:
            acct.priority = 10
        if sess.discovered_models:
            acct.models = list(sess.discovered_models)[:50]
        if sess.config.get("base_url"):
            acct.endpoint = sess.config["base_url"]
        acct.capabilities = self._resolve_capabilities(sess)
        registry.account_registry.register_account(acct)
        sess.registered = True
        sess.step = "register"

        # OmniRoute-style persistence & active routing pool registration for ALL providers
        if sess.provider_id == "antigravity":
            app_data_dir = (
                sess.config.get("app_data_dir")
                or (sess.account.metadata.get("data_dir") if sess.account else None)
                or (sess.account_id if sess.account_id.startswith("antigravity-account-") else f"antigravity-account-{sess.account_id.replace('antigravity-', '')}")
            )
            email = sess.config.get("email") or (sess.account.metadata.get("email") if sess.account else "")
            desc = f"Antigravity account ({email})" if email else f"Antigravity account {sess.account_id}"
            acct.description = desc
            account_conf = {
                "account_id": sess.account_id.replace("antigravity-", ""),
                "agent_id": sess.account_id,
                "display_name": acct.display_name,
                "description": desc,
                "priority": acct.priority,
                "enabled": True,
                "data_dir": app_data_dir,
                "models": acct.models or ["gemini-3.8-flash-medium", "gemini-3.7-flash-high", "gemini-3.7-flash-medium"],
                "default_model": (acct.models[0] if acct.models else "gemini-3.8-flash-medium"),
                "capabilities": acct.capabilities,
                "execution": {
                    "command": "~/.gemini/bin/agy",
                    "app_data_dir": app_data_dir,
                    "output_format": "json",
                    "dangerously_skip_permissions": False,
                    "default_timeout_seconds": 300,
                },
            }
            try:
                add_account_config("antigravity", sess.account_id, account_conf)
            except Exception:
                pass

            # Live-register adapter with Antigravity provider & AI bridge so router picks it up in real time
            try:
                prov = registry.get_provider("antigravity")
                if prov:
                    adapter_cfg = AntigravityAdapter._config_from_dict(
                        sess.account_id, account_conf,
                        command="~/.gemini/bin/agy",
                        fallback_commands=("~/.local/bin/agy", "~/.local/bin/antigravity", "/usr/local/bin/antigravity"),
                        profile_root=Path.home() / ".gemini",
                    )
                    adapter = AntigravityAccountAdapter(adapter_cfg)
                    prov.add_adapter(adapter)
                    registry.register_adapter("antigravity", adapter)
                    bridge = AgentProviderBridge(adapter)
                    registry.register_ai_provider(bridge)
            except Exception:
                pass

        elif sess.provider_id == "cline":
            config_dir = sess.account.metadata.get("config_dir") or str(Path.home() / ".mission-control" / "cline" / sess.account_id / "config")
            data_dir = sess.account.metadata.get("data_dir") or str(Path.home() / ".mission-control" / "cline" / sess.account_id / "data")
            email = sess.account.metadata.get("email") or UNKNOWN_ACCOUNT_EMAIL
            desc = f"Cline account ({email} - WorkOS OAuth)"
            acct.description = desc
            account_conf = {
                "account_id": sess.account_id.replace("cline-", ""),
                "agent_id": sess.account_id,
                "display_name": acct.display_name,
                "description": desc,
                "priority": acct.priority,
                "enabled": True,
                "config_dir": config_dir,
                "data_dir": data_dir,
                "capabilities": acct.capabilities,
                "models": acct.models or ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4-flash", "anthropic/claude-fable-5.1", "auto"],
                "default_model": (acct.models[0] if acct.models else "z-ai/glm-5.3-flash"),
            }
            try:
                add_account_config("cline", sess.account_id, account_conf)
            except Exception:
                pass
            try:
                prov = registry.get_provider("cline")
                if prov:
                    adapter = ClineAdapter(
                        agent_id=sess.account_id,
                        account_id=sess.account_id,
                        config_dir=config_dir,
                        data_dir=data_dir,
                        capabilities=frozenset(Capability(c) for c in acct.capabilities if c in [cap.value for cap in Capability]),
                        models=tuple(acct.models) if acct.models else ("auto",),
                    )
                    prov.add_adapter(adapter)
                    registry.register_adapter("cline", adapter)
                    bridge = AgentProviderBridge(adapter)
                    registry.register_ai_provider(bridge)
            except Exception:
                pass

        elif sess.provider_id == "kiro":
            auth_method = (sess.account.metadata.get("auth_method") if sess.account else None) or sess.auth_method or "local_session"
            region = (sess.account.metadata.get("region") if sess.account else None) or sess.config.get("region") or "us-east-1"
            start_url = (sess.account.metadata.get("start_url") if sess.account else None) or sess.config.get("start_url") or ""

            if auth_method == "idc":
                desc = f"Kiro (IAM Identity Center SSO - {region})"
            elif auth_method == "google":
                desc = f"Kiro (Google Social Login - {region})"
            elif auth_method == "github":
                desc = f"Kiro (GitHub Social Login - {region})"
            elif auth_method == "import":
                desc = f"Kiro (AWS SSO Cache Import - {region})"
            elif auth_method == "device_code":
                desc = f"Kiro (AWS Builder ID - {region})"
            elif auth_method == "api_key":
                desc = f"Kiro (Custom Session Key - {region})"
            else:
                desc = "Kiro CLI agent (Local Engine)"

            acct.description = desc
            account_conf = {
                "account_id": sess.account_id.replace("kiro-", ""),
                "agent_id": sess.account_id,
                "display_name": acct.display_name,
                "description": desc,
                "priority": acct.priority,
                "enabled": True,
                "auth_method": auth_method,
                "region": region,
                "capabilities": acct.capabilities,
                "models": acct.models or ["auto", "claude-opus-5", "claude-sonnet-5", "gpt-5.6-sol"],
                "default_model": (acct.models[0] if acct.models else "auto"),
            }
            if start_url:
                account_conf["start_url"] = start_url
            if sess.credential_reference:
                account_conf["credential_reference"] = sess.credential_reference

            try:
                add_account_config("kiro", sess.account_id, account_conf)
            except Exception:
                pass
            try:
                prov = registry.get_provider("kiro")
                if prov:
                    adapter = KiroAdapter(
                        agent_id=sess.account_id,
                        account_id=sess.account_id,
                    )
                    prov.add_adapter(adapter)
                    registry.register_adapter("kiro", adapter)
                    bridge = AgentProviderBridge(adapter)
                    registry.register_ai_provider(bridge)
            except Exception:
                pass

        else:
            pid = sess.provider_id
            base_url = sess.config.get("base_url") or APIProviderOnboarder.resolve_endpoint(pid)
            account_conf = {
                "account_id": sess.account_id,
                "agent_id": sess.account_id,
                "provider_id": pid,
                "display_name": acct.display_name,
                "priority": acct.priority,
                "enabled": True,
                "base_url": base_url,
                "credential_reference": sess.credential_reference or "",
                "capabilities": acct.capabilities,
                "models": acct.models or [],
                "default_model": (acct.models[0] if acct.models else "default"),
            }
            try:
                add_account_config(pid, sess.account_id, account_conf)
            except Exception:
                pass

            try:
                ai_prov = registry.get_ai_provider(pid)
                if not ai_prov:
                    if pid in ("gemini", "gemini-api"):
                        ai_prov = GeminiProvider(provider_id=pid, default_model=account_conf["default_model"])
                    elif pid == "anthropic":
                        ai_prov = AnthropicProvider(provider_id=pid, default_model=account_conf["default_model"])
                    elif pid == "ollama":
                        ai_prov = OllamaProvider(provider_id=pid, base_url=base_url, default_model=account_conf["default_model"])
                    else:
                        ai_prov = OpenAICompatibleProvider(
                            provider_id=pid,
                            base_url=base_url,
                            credential_reference=sess.credential_reference or "",
                            default_model=account_conf["default_model"],
                            models=tuple(acct.models) if acct.models else ("default",),
                            display_name=acct.display_name,
                        )
                    registry.register_ai_provider(ai_prov)

                prov = registry.get_provider(pid)
                if not prov:
                    prov = Provider(
                        id=pid,
                        name=pid.capitalize(),
                        description=f"{pid.capitalize()} Provider",
                        enabled=True,
                    )
                    registry.register_provider(prov)

                api_adapter = AIProviderAgentAdapter(
                    ai_provider=ai_prov,
                    agent_id=sess.account_id,
                    account_id=sess.account_id,
                    capabilities=frozenset(Capability(c) for c in acct.capabilities if c in [cap.value for cap in Capability]),
                    models=tuple(acct.models) if acct.models else (account_conf["default_model"],),
                    default_model=account_conf["default_model"],
                )
                prov.add_adapter(api_adapter)
                registry.register_adapter(pid, api_adapter)
            except Exception:
                pass

        wizard_event_stream.emit(
            "account.registered", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Account registered into registry", lifecycle_state=acct.lifecycle_state.value,
            extra={"credential_reference": sess.credential_reference or ""},
        )

    @staticmethod
    def _resolve_capabilities(sess: WizardSession) -> list[str]:
        """Return the router-valid capabilities for a newly onboarded account.

        Explicit configuration wins, but is filtered against the ``Capability``
        enum: the router silently discards unrecognised strings, so accepting
        them here would register an account that looks configured and is
        unroutable. Anything left empty falls back to a provider-appropriate
        default rather than to no capabilities at all.
        """
        requested = sess.config.get("capabilities") or []
        if isinstance(requested, str):
            requested = [requested]
        valid = {c.value for c in Capability}
        resolved = sorted({str(c) for c in requested if str(c) in valid})
        if resolved:
            return resolved

        pid = (sess.provider_id or "").lower().strip()
        if pid == "antigravity":
            defaults = WizardManager._DEFAULT_CAPABILITIES_ANTIGRAVITY
        elif pid == "cline":
            defaults = WizardManager._DEFAULT_CAPABILITIES_CLINE
        elif pid == "kiro":
            defaults = frozenset({
                Capability.TERMINAL_OPERATIONS,
                Capability.LOCAL_VALIDATION,
                Capability.BUILD_AND_TEST,
                Capability.CLOUD_READ_ONLY,
                Capability.KUBERNETES_READ_ONLY,
            })
        else:
            defaults = WizardManager._DEFAULT_CAPABILITIES_API
        return sorted(c.value for c in defaults)

    def health_check(self, sess: WizardSession) -> dict[str, Any]:
        sess.step = "health_check"
        wizard_event_stream.emit(
            "account.health_check", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Running health check", lifecycle_state=(sess.account.lifecycle_state.value if sess.account else None),
        )
        healthy = True
        reason = "Ready"
        if sess.provider_id == "cline":
            healthy = True
            reason = "Cline CLI profile configured"
        elif sess.provider_id == "kiro":
            exe = shutil.which("kiro-cli") or str(Path.home() / ".local" / "bin" / "kiro-cli")
            healthy = bool(shutil.which("kiro-cli") or Path(exe).exists())
            reason = "kiro-cli engine active" if healthy else "kiro-cli executable not found"
        elif sess.credential_reference:
            healthy = get_credential_manager().exists(sess.credential_reference)
            reason = "Credential present" if healthy else "Credential missing"
        return {"healthy": healthy, "reason": reason}

    def complete(self, sess: WizardSession) -> None:
        if sess.account is None:
            raise WizardError("No account to bring online", AccountLifecycleState.CONFIG_ERROR)
        self._transition(sess, AccountLifecycleState.ONLINE, reason="Online")
        sess.account.auth_state = AuthState.AUTHENTICATED
        sess.account.health_state = HealthState.HEALTHY
        sess.account.status = AccountStatus.ONLINE
        sess.step = "complete"
        sess.completed = True
        wizard_event_stream.emit(
            "account.online", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Account online", lifecycle_state="ONLINE",
        )
        with self._lock:
            self._sessions.pop(sess.wizard_id, None)

    def fail(self, sess: WizardSession, err: WizardError) -> None:
        """Record a failure by driving the account into the correct failure state."""
        sess.last_error = str(err)
        sess.failure_state = err.failure_state.value
        # Best-effort transition to the failure state (guarded by the machine).
        try:
            if sess.account is not None:
                self._transition(sess, err.failure_state, reason=str(err))
        except Exception:
            pass
        name = {
            AccountLifecycleState.AUTH_FAILED: "account.authentication_failed",
            AccountLifecycleState.AUTH_CANCELLED: "account.authentication_cancelled",
            AccountLifecycleState.VALIDATION_FAILED: "account.validation_failed",
            AccountLifecycleState.PROVIDER_UNAVAILABLE: "account.validation_failed",
            AccountLifecycleState.CONFIG_ERROR: "account.validation_failed",
        }.get(err.failure_state, "account.validation_failed")
        wizard_event_stream.emit(
            name, sess.correlation_id, sess.provider_id, sess.account_id,
            message=str(err), lifecycle_state=err.failure_state.value,
        )

    def cancel(self, sess: WizardSession) -> dict[str, Any]:
        """Roll back cleanly: no orphaned record, credential, or profile dir."""
        sess.cancelled = True
        # Drive lifecycle to AUTH_CANCELLED where legal, purely for auditability.
        try:
            if sess.account is not None and AccountLifecycleStateMachine.can_transition(
                sess.account.lifecycle_state, AccountLifecycleState.AUTH_CANCELLED
            ):
                self._transition(sess, AccountLifecycleState.AUTH_CANCELLED, reason="Cancelled by operator")
        except Exception:
            pass
        wizard_event_stream.emit(
            "account.authentication_cancelled", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Onboarding cancelled by operator", lifecycle_state="AUTH_CANCELLED",
        )

        cleanup: dict[str, Any] = {
            "account_record_removed": False,
            "reference_purged": False,
            "profile_dirs_removed": [],
        }
        # 1. If the account was registered, use the platform's safe removal which
        #    deletes the credential + isolated dirs + record with protected-profile
        #    guards. Otherwise clean up the pre-registration artifacts by hand.
        if sess.registered:
            try:
                result = registry.account_registry.safe_remove_account(
                    sess.account_id, correlation_id=sess.correlation_id, remove_directories=True,
                )
                rd = result.to_dict() if hasattr(result, "to_dict") else {}
                cleanup["account_record_removed"] = bool(rd.get("removed"))
                cleanup["reference_purged"] = bool(rd.get("credential_deleted"))
                cleanup["profile_dirs_removed"] = rd.get("directories_removed", [])
            except Exception as exc:
                cleanup["error"] = str(exc)
        else:
            # Not yet registered: purge any stored credential and any created dirs.
            if sess.credential_reference:
                try:
                    cleanup["reference_purged"] = bool(get_credential_manager().delete(sess.credential_reference))
                except Exception:
                    pass
            for d in sess.profile_dirs:
                try:
                    p = Path(d)
                    # Hard guard: never remove the protected IDE profile.
                    if p.name in ("antigravity-ide", "ide") or "antigravity-ide" in str(p):
                        continue
                    if p.is_dir():
                        import shutil as _shutil
                        _shutil.rmtree(p, ignore_errors=True)
                        cleanup["profile_dirs_removed"].append(str(p))
                except Exception:
                    pass
            # Guarantee no orphaned record exists even if something half-registered.
            if registry.account_registry.get_account(sess.account_id):
                try:
                    registry.account_registry.safe_remove_account(sess.account_id, remove_directories=True)
                    cleanup["account_record_removed"] = True
                except Exception:
                    pass

        if sess.provider_id == "antigravity":
            try:
                remove_account_config("antigravity", sess.account_id)
            except Exception:
                pass
            try:
                prov = registry.get_provider("antigravity")
                if prov:
                    prov.remove_adapter(sess.account_id)
            except Exception:
                pass

        wizard_event_stream.emit(
            "account.removed", sess.correlation_id, sess.provider_id, sess.account_id,
            message="Onboarding rolled back; no orphaned artifacts remain",
            lifecycle_state=(sess.account.lifecycle_state.value if sess.account else None),
            extra=cleanup,
        )
        with self._lock:
            self._sessions.pop(sess.wizard_id, None)
        return cleanup

    def back(self, sess: WizardSession) -> None:
        """Move one step back. Re-entrant steps transition backward where legal."""
        try:
            idx = WizardSession.ORDER.index(sess.step)
        except ValueError:
            idx = 0
        if idx <= 0:
            return
        prev = WizardSession.ORDER[idx - 1]
        # Reflect backward lifecycle where the machine permits (e.g. back to
        # CONFIGURING from AUTHENTICATING/AUTHENTICATED). This is best-effort.
        back_state = {
            "configure": AccountLifecycleState.CONFIGURING,
            "authenticate": AccountLifecycleState.CONFIGURING,
            "validate": AccountLifecycleState.CONFIGURING,
        }.get(sess.step)
        if back_state and sess.account is not None:
            try:
                if AccountLifecycleStateMachine.can_transition(sess.account.lifecycle_state, back_state):
                    self._transition(sess, back_state, reason="Stepped back")
            except Exception:
                pass
        sess.step = prev
        sess.last_error = None
        sess.failure_state = None


wizard_manager = WizardManager()

AUTH_TOKEN_ENV_VAR = "MISSION_CONTROL_AUTH_TOKEN"
AUTH_TOKEN_FILE = PROJECT_ROOT / "runtime" / "mission_control.token"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "img-src 'self' data:;"
    ),
}


def get_or_create_auth_token() -> str:
    """Retrieve the mission control auth token from env or disk, or generate one."""
    env_token = os.environ.get(AUTH_TOKEN_ENV_VAR)
    if env_token and env_token.strip():
        return env_token.strip()

    if AUTH_TOKEN_FILE.is_file():
        try:
            token = AUTH_TOKEN_FILE.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    token = secrets.token_urlsafe(32)
    AUTH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        os.chmod(AUTH_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


class RateLimiter:
    """Sliding-window in-memory rate limiter."""

    def __init__(self, max_requests: int = 30, window_seconds: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, client_id: str = "global") -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            timestamps = self._requests.setdefault(client_id, [])
            self._requests[client_id] = [t for t in timestamps if t > cutoff]
            if len(self._requests[client_id]) >= self.max_requests:
                return False
            self._requests[client_id].append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._requests.clear()


execution_rate_limiter = RateLimiter(max_requests=30, window_seconds=60.0)


def is_allowed_origin(origin: str | None) -> bool:
    """Enforce CORS policy: only local loopback origins are permitted."""
    if not origin:
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
        if parsed.scheme in ("http", "https"):
            hostname = parsed.hostname
            if hostname in ("127.0.0.1", "localhost", "::1"):
                return True
    except Exception:
        pass
    return False


ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_allowed_host_header(host: str | None) -> bool:
    """Reject non-loopback Host headers (DNS-rebinding defence).

    A rebinding page (``http://attacker.example:3333`` re-resolved to 127.0.0.1)
    is same-origin to the browser, so its GETs carry no Origin header and would
    otherwise be able to read the public ``/api/token`` handshake. Browsers
    always send the page's own hostname in ``Host``, so pinning it to loopback
    closes that path. A missing Host (raw HTTP/1.0 client) cannot come from a
    browser and is allowed.
    """
    if host is None:
        return True
    try:
        hostname = urllib.parse.urlsplit(f"//{host.strip()}").hostname
    except ValueError:
        return False
    return hostname in ALLOWED_LOOPBACK_HOSTS


class SecurityError(RuntimeError):
    """Raised when a local boundary or security invariant is violated."""


def validate_host_binding(host: str) -> str:
    """Enforce strict local-only network boundary."""
    if host not in ALLOWED_LOOPBACK_HOSTS:
        raise SecurityError(
            f"Security violation: Binding to non-local interface '{host}' is strictly forbidden. "
            f"Mission Control must remain bound to local loopback ({', '.join(sorted(ALLOWED_LOOPBACK_HOSTS))})."
        )
    return host


# Upper bound for JSON request bodies. Every legitimate payload (task
# instructions, memory notes, wizard config) is far smaller; without a cap a
# single request could make the server buffer an arbitrary Content-Length.
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024

PUBLIC_GET_PATHS = frozenset({
    "/",
    # Browsers request /favicon.ico unconditionally and without the Authorization
    # header, so gating it only produced a 401 console error on every page load.
    # It carries no data.
    "/favicon.ico",
    "/callback",
    "/api/health",
    "/api/status",
    "/api/token",
    "/api/oauth/cline/callback",
    # /api/oauth/kiro/auto-import is deliberately NOT public: it is an API call
    # made by the authenticated UI (not a browser redirect target), and it
    # spawns `kiro-cli whoami` and returns the signed-in email / SSO start URL.
})


def is_public_path(path: str) -> bool:
    """Determine if a GET path is accessible without Bearer token authentication."""
    if path in PUBLIC_GET_PATHS:
        return True
    if path.startswith("/static/"):
        return True
    if path.startswith("/api/oauth/") and path.endswith("/callback"):
        return True
    if path.startswith("/api/oauth/callback"):
        return True
    return False


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # listen() backlog. The socketserver default of 5 reset connections as soon
    # as a few dozen UI polls / SSE clients arrived at once.
    request_queue_size = 128

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: Any,
        bind_and_activate: bool = True,
    ) -> None:
        validate_host_binding(server_address[0])
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)


class MissionControlHandler(BaseHTTPRequestHandler):
    server_version = "AgenticBrain-MissionControl/2.0"

    def address_string(self) -> str:
        return str(self.client_address[0]) if hasattr(self, "client_address") else "127.0.0.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _apply_security_headers(self) -> None:
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        origin = self.headers.get("Origin")
        if origin and is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Access-Control-Max-Age", "86400")

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        bad_host = not is_allowed_host_header(self.headers.get("Host"))
        if bad_host or (origin and not is_allowed_origin(origin)):
            message = "Disallowed Host header" if bad_host else "Disallowed cross-origin request"
            out = json.dumps({"error": "Forbidden", "message": message}).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self._apply_security_headers()
            self.end_headers()
            self.wfile.write(out)
            return False
        return True

    def _verify_auth(self, path: str, allow_query_token: bool = False) -> bool:
        auth_header = self.headers.get("Authorization")
        client_ip = self.client_address[0] if hasattr(self, "client_address") else "127.0.0.1"
        token = None

        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif allow_query_token and "?" in self.path:
            query = self.path.split("?", 1)[1]
            params = urllib.parse.parse_qs(query)
            token_candidates = params.get("token") or params.get("access_token")
            if token_candidates:
                token = token_candidates[0].strip()

        valid = False
        if token:
            expected = get_or_create_auth_token()
            if hmac.compare_digest(token, expected):
                valid = True

        if valid:
            event_bus.emit(
                Event(
                    event_type=EventType.AUTH_SUCCESS,
                    metadata={"path": path, "client": client_ip},
                )
            )
            return True
        else:
            event_bus.emit(
                Event(
                    event_type=EventType.AUTH_FAILURE,
                    metadata={"path": path, "client": client_ip},
                )
            )
            out = json.dumps(
                {
                    "error": "Unauthorized",
                    "message": f"Valid Bearer token required for {path}",
                }
            ).encode("utf-8")
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("WWW-Authenticate", 'Bearer realm="MissionControl"')
            self._apply_security_headers()
            # FIX (BUG-001, full-system validation): terminate the header block.
            # Without end_headers() the buffered status line and headers are
            # never flushed, so clients receive a bare JSON body with no HTTP
            # status line (BadStatusLine) instead of a clean 401 response.
            self.end_headers()
            try:
                self.wfile.write(out)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return False

    def _dispatch(self, handler: Any) -> None:
        try:
            handler()
        except BadRequest as exc:
            self._serve_json({"error": "Bad Request", "message": str(exc)}, status=400)

    def _read_json_body(self) -> dict[str, Any] | None:
        """Read and parse a bounded JSON request body.

        Returns the parsed object (``{}`` for an empty body), or ``None`` after
        answering 400/413 itself when Content-Length is invalid or too large or
        the body is not a JSON object.
        """
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw_len)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            self._serve_json({"error": "Invalid Content-Length"}, status=400)
            return None
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            self._serve_json(
                {"error": "Payload Too Large", "max_bytes": MAX_REQUEST_BODY_BYTES},
                status=413,
            )
            return None
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._serve_json({"error": "Bad Request", "message": "Malformed JSON body"}, status=400)
            return None
        if not isinstance(payload, dict):
            self._serve_json({"error": "Bad Request", "message": "JSON body must be an object"}, status=400)
            return None
        return payload

    def _serve_json(self, data: Any, status: int = 200, redact: bool = True) -> None:
        # Phase 10: Apply SecretRedactor to all API response payloads.
        #
        # `redact=False` is reserved for the loopback session-auth handshake
        # (GET /api/token). That token is this server's own CSRF/bearer nonce,
        # not a provider credential, and the browser UI cannot authenticate
        # without reading it back verbatim. Never pass redact=False for any
        # payload that can carry provider credentials.
        if redact and isinstance(data, (dict, list)):
            sanitized = redactor.redact_dict(data)
        else:
            sanitized = data
        out = json.dumps(sanitized, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self._apply_security_headers()
        self.end_headers()
        try:
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, path: str) -> None:
        rel = path.replace("/static/", "").lstrip("/")
        file_path = (STATIC_DIR / rel).resolve()
        # is_relative_to, not a string prefix test: "static_x/..." shares the
        # "static" prefix but lies outside STATIC_DIR.
        if not file_path.is_relative_to(STATIC_DIR):
            self.send_response(403)
            self._apply_security_headers()
            self.end_headers()
            return

        if file_path.is_file():
            self.send_response(200)
            if rel.endswith(".js"):
                self.send_header("Content-Type", "application/javascript")
            elif rel.endswith(".css"):
                self.send_header("Content-Type", "text/css")
            elif rel.endswith(".woff2"):
                self.send_header("Content-Type", "font/woff2")
            elif rel.endswith(".png"):
                self.send_header("Content-Type", "image/png")
            elif rel.endswith(".jpg") or rel.endswith(".jpeg"):
                self.send_header("Content-Type", "image/jpeg")
            elif rel.endswith(".svg"):
                self.send_header("Content-Type", "image/svg+xml")
            elif rel.endswith(".json") or rel.endswith(".tmj"):
                self.send_header("Content-Type", "application/json")
            try:
                self.send_header("Content-Length", str(file_path.stat().st_size))
            except OSError:
                pass
            self._apply_security_headers()
            self.end_headers()
            try:
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_response(404)
            self._apply_security_headers()
            self.end_headers()

    def _get_git_info(self) -> dict[str, Any]:
        try:
            branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(PROJECT_ROOT), text=True).strip()
            commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(PROJECT_ROOT), text=True).strip()
        except Exception:
            branch, commit = "main", "initial"
        try:
            status = subprocess.check_output(["git", "status", "--short"], cwd=str(PROJECT_ROOT), text=True).strip()
            status_text = f"{len(status.splitlines())} uncommitted change(s)" if status else "clean"
        except Exception:
            status_text = "unknown"
        return {"branch": branch, "commit": commit, "status": status_text}
    def _get_agents_runtime(self, all_tasks: list[Task] | None = None) -> dict[str, Any]:
        raw_providers = registry.to_dict()
        agent_provider_ids = {"antigravity", "kiro", "cline", "openhands"}
        providers = {k: v for k, v in raw_providers.items() if k in agent_provider_ids or v.get("type") == "agent"}
        if all_tasks is None:
            all_tasks = task_manager.list_tasks()
        sessions = orchestrator.sessions.list_recent(200)
        events = event_bus.get_recent_events(400)
        active_ids = set(orchestrator.swarm.get_active_task_ids())

        for provider in providers.values():
            for agent_id, account in provider["accounts"].items():
                agent_tasks = [t for t in all_tasks if t.assigned_agent == agent_id]
                agent_sessions = [s for s in sessions if s.agent_id == agent_id]
                agent_events = [e for e in events if e.agent_id == agent_id]

                running = [t for t in agent_tasks if t.status == TaskStatus.RUNNING and t.task_id in active_ids]
                completed = [t for t in agent_tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.VERIFICATION_COMPLETE)]
                failed = [t for t in agent_tasks if t.status in (TaskStatus.FAILED, TaskStatus.DEPTH_LIMIT_REACHED, TaskStatus.BUDGET_EXHAUSTED, TaskStatus.CANCELLED)]
                finished = completed + failed
                success_rate = (len(completed) / len(finished) * 100.0) if finished else 100.0
                durations = [t.duration_seconds for t in finished if t.duration_seconds and t.duration_seconds > 0]
                avg_latency = (sum(durations) / len(durations)) if durations else 0.0

                token_metrics = orchestrator.swarm.token_tracker.get_metrics()
                token_info = token_metrics.get("by_agent", {}).get(agent_id, {})

                health_reason = account.get("health_reason", "")
                if not account.get("healthy"):
                    if "Missing credential" in health_reason or "AUTH_ERROR" in health_reason or not account.get("credential_reference"):
                        display_status = "NOT_CONFIGURED"
                    else:
                        display_status = "OFFLINE"
                elif running:
                    display_status = "WORKING"
                elif agent_tasks:
                    display_status = "IDLE"
                else:
                    display_status = "ONLINE"

                running_task = running[0] if running else None
                latest = agent_tasks[0] if agent_tasks else None
                latest_session = agent_sessions[0] if agent_sessions else None

                # Extract historical errors cleanly attributed to past tasks
                historical_errors = []
                for t in agent_tasks:
                    for err in t.errors:
                        historical_errors.append({
                            "error": str(err),
                            "task_id": t.task_id,
                            "task_status": t.status.value,
                            "timestamp": getattr(t, "updated_at", getattr(t, "created_at", None)),
                        })

                account.update(
                    {
                        "display_status": display_status,
                        "success_rate": round(success_rate, 1),
                        "avg_latency": round(avg_latency, 2),
                        "known_tokens": token_info.get("known_tokens", 0),
                        "estimated_tokens": token_info.get("estimated_tokens", 0),
                        "unknown_usage_runs": token_info.get("unknown_usage_runs", 0),
                        "token_tasks": token_info.get("tasks", 0),
                        "capabilities": account.get("capabilities", []),
                        "models": account.get("models", []),
                        "default_model": account.get("default_model", "auto"),
                        "model_capabilities": provider.get("model_capabilities", []),
                        "current_task": {
                            "task_id": running_task.task_id,
                            "title": running_task.title,
                            "status": running_task.status.value,
                            "stage": getattr(running_task, "stage", "QUEUED"),
                            "requested_model": running_task.assigned_model,
                            "reported_model": running_task.actual_model,
                            "duration_seconds": running_task.duration_seconds,
                        }
                        if running_task
                        else None,
                        "last_task": {
                            "task_id": latest.task_id,
                            "title": latest.title,
                            "status": latest.status.value,
                            "stage": getattr(latest, "stage", "QUEUED"),
                            "requested_model": latest.assigned_model,
                            "reported_model": latest.actual_model,
                            "duration_seconds": latest.duration_seconds,
                        }
                        if latest
                        else None,
                        "session_id": latest_session.session_id if latest_session else None,
                        "conversation_id": latest_session.conversation_id if latest_session else None,
                        "last_activity": (
                            agent_events[0].timestamp if agent_events else
                            (latest_session.updated_at if latest_session else None)
                        ),
                        "counts": {
                            "total": len(agent_tasks),
                            "running": len(running),
                            "completed": len(completed),
                            "failed": len(failed),
                        },
                        "recent_tasks": [
                            {
                                "task_id": t.task_id,
                                "title": t.title,
                                "status": t.status.value,
                                "stage": getattr(t, "stage", "QUEUED"),
                                "requested_model": t.assigned_model,
                                "reported_model": t.actual_model,
                                "conversation_id": t.conversation_id,
                                "duration_seconds": t.duration_seconds,
                                "files": t.files,
                                "handoffs": t.handoffs,
                                "fallback": t.result.get("fallback") if isinstance(t.result, dict) else None,
                            }
                            for t in agent_tasks[:10]
                        ],
                        "recent_errors": [
                            err for t in agent_tasks[:10] for err in t.errors
                        ][:5],
                        "historical_errors": historical_errors[:5],
                        "last_task_error": historical_errors[0] if historical_errors else None,
                        "last_error": account.get("health_reason") or None,
                        "recent_events": [
                            {
                                "event_type": e.event_type.value,
                                "timestamp": e.timestamp,
                                "task_id": e.task_id,
                            }
                            for e in agent_events[:10]
                        ],
                    }
                )
        return providers

    def _compute_account_metrics(self, accounts: list) -> dict[str, int]:
        # ── Phase 22 Part 8: Granular, decoupled account metrics ──────────
        #
        # ROOT CAUSE of the misleading "4 Online" agent count (traced &
        # confirmed empirically, correcting/confirming Audit Defect 1):
        #
        # There was never a real "4 online agents" figure. Two independent
        # conflations produced the "4":
        #   1. PROVIDER-vs-ACCOUNT conflation. The Mission Control banner
        #      rendered `providers_online / providers_total`, and
        #      `providers_total` == 4 because exactly 4 PROVIDERS are
        #      registered (antigravity, cline, kiro, openai) even though those
        #      4 providers own 8 ACCOUNTS. A provider count was displayed where
        #      operators read it as an agent/account count -> "X / 4".
        #   2. ADAPTER-list conflation for "workers". `active_workers` was
        #      derived from `registry.list_active_adapters()`, which only
        #      enumerates agent `Provider.adapters` (antigravity x3, kiro,
        #      cline x3 = 7) and completely omits direct-API accounts that live
        #      in `_ai_providers` (e.g. `openai`). So neither the "4" nor the
        #      worker count was ever an honest count of live accounts.
        #
        # The single ambiguous number conflated lifecycle, provider identity,
        # and adapter registration. The fix below computes EIGHT explicit
        # metrics, each from ONE correct orthogonal field on the account, never
        # from a provider total, an adapter list, or a conflated status string.
        #
        # Field mapping (values are the string enum values from Account.to_dict):
        #   Registered    -> every account known to the registry
        #   Authenticated -> auth_state == AUTHENTICATED
        #   Healthy       -> health_state == HEALTHY
        #   Online        -> lifecycle_state == ONLINE
        #   Busy          -> task_state in {RUNNING, ASSIGNED}
        #   Idle          -> lifecycle ONLINE and task_state == IDLE
        #   Offline       -> lifecycle_state == OFFLINE
        #   Disabled      -> lifecycle_state == DISABLED or enabled is False
        def _state(acc: Any, attr: str) -> str:
            val = getattr(acc, attr, "")
            return val.value if hasattr(val, "value") else str(val)

        registered = len(accounts)
        authenticated = 0
        healthy = 0
        online = 0
        busy = 0
        idle = 0
        offline = 0
        disabled = 0
        for a in accounts:
            lifecycle = _state(a, "lifecycle_state")
            auth = _state(a, "auth_state")
            health = _state(a, "health_state")
            task = _state(a, "task_state")
            enabled = getattr(a, "enabled", True)

            if auth == "AUTHENTICATED":
                authenticated += 1
            if health == "HEALTHY":
                healthy += 1
            if lifecycle == "ONLINE":
                online += 1
            if task in ("RUNNING", "ASSIGNED"):
                busy += 1
            if lifecycle == "ONLINE" and task == "IDLE":
                idle += 1
            if lifecycle == "OFFLINE":
                offline += 1
            if lifecycle == "DISABLED" or enabled is False:
                disabled += 1

        return {
            "registered": registered,
            # NOTE: this metric counts auth_state == AUTHENTICATED. The JSON key
            # deliberately avoids the substring "auth" because the response-wide
            # SecretRedactor (owned by another module, not editable here) redacts
            # ANY key matching /auth|token|secret|credential.../ to
            # "***REDACTED***". Keying it "authenticated" would turn the integer
            # count into a redacted string in the browser. "signed_in" carries
            # the identical meaning and is rendered under the label
            # "Authenticated" in the UI.
            "signed_in": authenticated,
            "healthy": healthy,
            "online": online,
            "busy": busy,
            "idle": idle,
            "offline": offline,
            "disabled": disabled,
        }

    def _get_system_status(self) -> dict[str, Any]:
        global _system_status_cache, _system_status_cache_time
        now = time.time()
        with _system_status_lock:
            if _system_status_cache is not None and (now - _system_status_cache_time < 2.0):
                return dict(_system_status_cache)

        tasks = task_manager.list_tasks()
        active_ids = set(orchestrator.swarm.get_active_task_ids())
        # Count every RUNNING task, not only those owned by this process's swarm
        # pool. Tasks dispatched by `brain swarm dispatch` execute in a separate
        # runner, so they are never in get_active_task_ids() — intersecting on it
        # pinned "Task Pipeline" to 0 Running while the Kanban correctly showed
        # them, which read as the dashboard being broken.
        running = [t for t in tasks if t.status == TaskStatus.RUNNING]
        active_tasks = [t.to_dict() for t in running]
        # Retained as a distinct signal: which running tasks this process owns.
        in_process_tasks = [t.to_dict() for t in running if t.task_id in active_ids]
        completed_tasks = [t.to_dict() for t in tasks if t.status in (TaskStatus.COMPLETED, TaskStatus.VERIFICATION_COMPLETE)]
        ready_tasks = [t.to_dict() for t in tasks if t.status in (TaskStatus.READY, TaskStatus.BACKLOG)]
        failed_tasks = [t.to_dict() for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.DEPTH_LIMIT_REACHED, TaskStatus.BUDGET_EXHAUSTED, TaskStatus.CANCELLED)]
        token_metrics = orchestrator.swarm.token_tracker.get_metrics()

        all_providers = registry.list_providers()
        all_ai = registry.list_ai_providers()
        all_pids = set(p.id for p in all_providers).union(p.provider_id for p in all_ai)
        all_accounts = registry.account_registry.list_accounts()
        healthy_accounts = [a for a in all_accounts if a.is_available()]
        all_models = registry.model_registry.list_models()
        available_models = [m for m in all_models if m.enabled]
        active_workers = [a.agent_id for a in registry.list_active_adapters()]

        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        total_usage = get_usage_tracker().get_summary()
        today_usage = get_usage_tracker().get_summary(day=today_str)
        known_tokens_total = token_metrics.get("known_tokens", 0) or token_metrics.get("total_known_tokens", 0)
        # "Today" must mean today: falling back to lifetime totals when nothing ran
        # today made the Overview show all-time tokens and cost as today's.
        today_metrics = orchestrator.swarm.token_tracker.get_metrics(day=today_str)
        today_known_tokens = today_metrics.get("known_tokens", 0) or 0
        today_tokens_val = today_usage.get("total_tokens", 0) or today_known_tokens
        today_cost_val = today_usage.get("estimated_cost_usd", 0.0) or 0.0
        total_cost_val = total_usage.get("estimated_cost_usd", 0.0)
        total_tokens_val = total_usage.get("total_tokens", 0) or known_tokens_total
        all_jobs = job_manager.list_jobs(limit=100)
        active_jobs = [j for j in all_jobs if j.status in ("pending", "running")]
        completed_jobs = [j for j in all_jobs if j.status == "completed"]
        failed_jobs = [j for j in all_jobs if j.status == "failed"]
        router = SmartRouter(registry)
        recent_routing = router.get_routing_history(limit=5)

        # Phase 22 Part 8: eight explicit, independently-computed account metrics
        account_metrics = self._compute_account_metrics(all_accounts)

        # Fast cached provider health check count
        providers_online = 0
        for p in all_providers:
            h = get_cached_provider_health(p)
            if (isinstance(h, dict) and h.get("healthy")) or (isinstance(h, tuple) and h[0]):
                providers_online += 1
        for p in all_ai:
            h = get_cached_provider_health(p)
            if (isinstance(h, tuple) and h[0]) or (isinstance(h, dict) and h.get("healthy")):
                providers_online += 1

        mc_overview = {
            "providers_online": providers_online,
            "providers_total": len(all_pids),
            "accounts_healthy": len(healthy_accounts),
            "accounts_total": len(all_accounts),
            "account_metrics": account_metrics,
            "models_available": len(available_models),
            "active_workers_count": len(active_workers),
            "active_workers_list": active_workers,
            "active_jobs": len(active_jobs),
            "completed_jobs": len(completed_jobs),
            "failed_jobs": len(failed_jobs),
            "today_tokens": today_tokens_val,
            "today_cost": today_cost_val,
            "today_usage_tokens": today_tokens_val,
            "today_estimated_cost_usd": today_cost_val,
            "total_tokens": total_tokens_val,
            "total_cost": total_cost_val,
            "known_tokens": known_tokens_total,
            "recent_routing": recent_routing,
        }

        res = {
            "status": "RUNNING",
            "tasks_count": len(tasks),
            "running_tasks": len(active_tasks),
            "in_process_running_tasks": len(in_process_tasks),
            "completed_tasks": len(completed_tasks),
            "ready_tasks": len(ready_tasks),
            "failed_tasks": len(failed_tasks),
            "registered_accounts": len(all_accounts),
            "healthy_accounts": len(healthy_accounts),
            "active_workers": len(active_workers),
            "active_workers_list": active_workers,
            "active_tasks": active_tasks,
            "recent_completed": completed_tasks[0] if completed_tasks else None,
            "memories_count": memory_store.count(),
            "agents": self._get_agents_runtime(all_tasks=tasks),
            "token_metrics": token_metrics,
            "mission_control": mc_overview,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with _system_status_lock:
            _system_status_cache = res
            _system_status_cache_time = time.time()
        return res

    def _get_overview(self) -> dict[str, Any]:
        status = self._get_system_status()
        active_tasks = status.get("active_tasks", [])
        total_tasks = status.get("tasks_count", 0)

        return {
            "status": "RUNNING",
            "host": {
                "os": "CachyOS Linux",
                "cpu_cores": 2,
                "memory_gb": 16,
                "max_concurrent_agents": 2,
                "max_heavy_agents": 1,
            },
            "task_counts": {
                "total": total_tasks,
                "running": len(active_tasks),
                "ready": status.get("ready_tasks", 0),
                "completed": status.get("completed_tasks", 0),
                "failed": status.get("failed_tasks", 0),
            },
            "registered_accounts": status.get("registered_accounts", 0),
            "healthy_accounts": status.get("healthy_accounts", 0),
            "active_workers": status.get("active_workers", 0),
            "active_workers_list": status.get("active_workers_list", []),
            "active_tasks": active_tasks,
            "recent_completed": status.get("recent_completed"),
            "token_metrics": status.get("token_metrics", {}),
            "agents": status.get("agents", {}),
            "git": self._get_git_info(),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "mission_control": status.get("mission_control", {}),
        }

    def do_OPTIONS(self) -> None:
        if not self._check_origin():
            return
        self.send_response(204)
        self._apply_security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch(self._handle_GET)

    def _handle_GET(self) -> None:
        if not self._check_origin():
            return

        # Dynamically sync any changes from config/providers.json
        ensure_registry_synced()

        path = self.path.split("?")[0]

        if not is_public_path(path):
            is_sse = (path == "/api/events/stream")
            if not self._verify_auth(path, allow_query_token=is_sse):
                return

        if path == "/":
            self._serve_html()
        elif path == "/callback":
            self._handle_oauth_callback()
        elif path == "/api/oauth/cline/callback":
            self._handle_cline_oauth_callback()
        elif path == "/api/oauth/kiro/auto-import":
            self._handle_kiro_auto_import()
        elif path.startswith("/static/"):
            self._serve_static(path)
        elif path == "/api/overview":
            self._serve_json(self._get_overview())
        elif path == "/api/status":
            self._serve_json(self._get_system_status())
        elif path == "/api/agents":
            self._serve_json(self._get_agents_runtime())
        elif path == "/api/office/state":
            # Auth-required (not in PUBLIC_GET_PATHS). Keyed on BRAIN_DIR so a
            # relocated store is never served from a stale snapshot.
            try:
                snapshot = _office_cache.get(str(brain_dir()), _build_office_state)
            except Exception:
                logger.warning("office state snapshot failed", exc_info=True)
                self._serve_json({"error": "Office state unavailable"}, status=500)
            else:
                self._serve_json(snapshot)
        elif path == "/api/sessions":
            self._serve_json(
                {"sessions": [s.to_dict() for s in orchestrator.sessions.list_recent(50)]}
            )
        elif path == "/api/health":
            self._serve_json(orchestrator.health(deep=False))
        elif path == "/api/tasks":
            tasks = [t.to_dict() for t in task_manager.list_tasks()]
            self._serve_json({"tasks": tasks})
        elif path == "/api/task":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            task_id = params.get("task_id", [None])[0]
            if not task_id:
                self._serve_json({"error": "Missing task_id query parameter"}, status=400)
            else:
                task = task_manager.get_task(task_id)
                if task:
                    self._serve_json({"task": task.to_dict()})
                else:
                    self._serve_json({"error": f"Task '{task_id}' not found"}, status=404)
        elif path == "/api/events":
            evts = [e.to_dict() for e in event_bus.get_recent_events(100)]
            self._serve_json({"events": evts})

        # ── Phase 22 Parts 6/9: Account Add Wizard discovery endpoints ──────
        # NOTE: response keys deliberately avoid the substring "auth" (e.g.
        # login_methods, not auth_methods) so the SecretRedactor — which scrubs
        # any key matching /auth/ — does not blank out this non-secret data. The
        # method values are plain enum strings such as "api_key" / "oauth".
        elif path == "/api/wizard/providers":
            entries = []
            seen = set()
            for p in registry.list_providers():
                seen.add(p.id)
                entries.append({
                    "id": p.id,
                    "name": p.name,
                    "type": ("IDE" if p.id == "antigravity" else "AGENT"),
                    "account_count": len(registry.account_registry.list_accounts(p.id)),
                    "login_methods": wizard_manager.auth_methods_for(p.id),
                })
            for ai_prov in registry.list_ai_providers():
                if ai_prov.provider_id not in seen:
                    seen.add(ai_prov.provider_id)
                    entries.append({
                        "id": ai_prov.provider_id,
                        "name": ai_prov.display_name,
                        "type": ai_prov.provider_type.value.upper(),
                        "account_count": len(registry.account_registry.list_accounts(ai_prov.provider_id)),
                        "login_methods": wizard_manager.auth_methods_for(ai_prov.provider_id),
                    })
            # Standard preset providers available to onboard anytime
            KNOWN_PRESETS = [
                ("antigravity", "Google Antigravity", "IDE"),
                ("openai", "OpenAI", "API"),
                ("anthropic", "Anthropic Claude", "API"),
                ("gemini", "Google Gemini", "API"),
                ("openrouter", "OpenRouter", "GATEWAY"),
                ("groq", "Groq Cloud", "API"),
                ("cline", "Cline", "AGENT"),
                ("kiro", "Kiro", "AGENT"),
                ("ollama", "Ollama (local)", "LOCAL_MODEL"),
            ]
            for pid, name, ptype in KNOWN_PRESETS:
                if pid not in seen:
                    seen.add(pid)
                    entries.append({
                        "id": pid,
                        "name": name,
                        "type": ptype,
                        "account_count": len(registry.account_registry.list_accounts(pid)),
                        "login_methods": wizard_manager.auth_methods_for(pid),
                    })
            self._serve_json({"providers": entries, "count": len(entries)})
        elif path in ("/api/wizard/login-methods", "/api/wizard/auth-methods"):
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0] or params.get("provider_id", [None])[0]
            if not prov:
                self._serve_json({"error": "Missing provider query parameter"}, status=400)
            else:
                methods = wizard_manager.auth_methods_for(prov)
                self._serve_json({
                    "provider_id": prov,
                    "login_methods": methods,
                    "supported": bool(methods),
                })
        elif path == "/api/wizard/next-account-id":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0] or params.get("provider_id", [None])[0] or "antigravity"
            next_id = wizard_manager.next_account_id(prov)
            self._serve_json({
                "provider_id": prov,
                "next_account_id": next_id,
            })
        elif path == "/api/wizard/events":
            self._serve_json({"events": wizard_event_stream.recent(100)})
        elif path == "/api/wizard/check-auth":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            wid = params.get("wizard_id", [None])[0]
            sess = wizard_manager.get(wid) if wid else None
            if not sess:
                self._serve_json({"error": "Wizard session not found"}, status=404)
            else:
                self._serve_json(_redact_preserving_booleans(wizard_manager.check_auth_status(sess)), redact=False)

        elif path == "/api/events/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._apply_security_headers()
            self.end_headers()

            event_queue: queue.Queue = queue.Queue(maxsize=250)
            redactor = get_credential_manager().redactor

            def _stream_listener(evt: Event) -> None:
                try:
                    payload = redactor.redact_dict(evt.to_dict())
                    event_queue.put_nowait(payload)
                except Exception:
                    pass

            event_bus.subscribe(_stream_listener)
            # Phase 22 Part 10: also stream redacted account/auth lifecycle events
            # (account.create_started ... account.online / account.removed), each
            # carrying the onboarding-flow correlation id. Payloads are already
            # redacted by WizardEventStream; we redact again as defense in depth.
            wizard_queue = wizard_event_stream.subscribe()

            # Send initial backlog of recent 25 events (oldest to newest)
            try:
                recent = event_bus.get_recent_events(25)
                for rev in reversed(recent):
                    rd = redactor.redact_dict(rev.to_dict())
                    msg = f"data: {json.dumps(rd)}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                for wev in wizard_event_stream.recent(25):
                    msg = f"data: {json.dumps(redactor.redact_dict(wev))}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
            except Exception:
                event_bus.unsubscribe(_stream_listener)
                wizard_event_stream.unsubscribe(wizard_queue)
                return

            try:
                while True:
                    wrote = False
                    try:
                        item = event_queue.get_nowait()
                        msg = f"data: {json.dumps(item)}\n\n"
                        self.wfile.write(msg.encode("utf-8"))
                        wrote = True
                    except queue.Empty:
                        pass
                    try:
                        witem = wizard_queue.get_nowait()
                        msg = f"data: {json.dumps(redactor.redact_dict(witem))}\n\n"
                        self.wfile.write(msg.encode("utf-8"))
                        wrote = True
                    except queue.Empty:
                        pass
                    if wrote:
                        self.wfile.flush()
                    else:
                        # Idle: block briefly on the primary bus, then loop so the
                        # wizard queue is checked promptly too.
                        try:
                            item = event_queue.get(timeout=2.0)
                            msg = f"data: {json.dumps(item)}\n\n"
                            self.wfile.write(msg.encode("utf-8"))
                            self.wfile.flush()
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                event_bus.unsubscribe(_stream_listener)
                wizard_event_stream.unsubscribe(wizard_queue)
        elif path == "/api/memory":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            search_val = params.get("search", [None])[0]
            scope_val = params.get("scope", [None])[0]
            scope_enum = None
            if scope_val:
                try:
                    scope_enum = MemoryScope(scope_val.upper())
                except Exception:
                    pass
            imp_val = params.get("importance", [None])[0]
            min_imp = int(imp_val) if imp_val and imp_val.isdigit() else None
            t_id = params.get("task_id", [None])[0]
            src_agent = params.get("agent_id", [None])[0]
            mems = [
                m.to_dict()
                for m in memory_store.query_memories(
                    search=search_val,
                    scope=scope_enum,
                    min_importance=min_imp,
                    task_id=t_id,
                    source_agent=src_agent,
                    limit=100,
                )
            ]
            self._serve_json({"memories": mems, "count": len(mems), "total": memory_store.count()})
        elif path == "/api/memory/retrieval-history":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            t_id = params.get("task_id", [None])[0]
            lim = _as_int(params.get("limit", [100])[0], "limit", 100)
            self._serve_json({"retrievals": memory_store.list_retrievals(limit=lim, task_id=t_id)})
        elif path == "/api/handoff":
            content = handoff_manager.get_current_handoff() or "No active handoff available."
            rec = handoff_manager.get_current_record()
            self._serve_json({
                "markdown": content,
                "record": rec.to_dict() if rec else None,
            })
        elif path == "/api/handoff/history":
            self._serve_json({"history": handoff_manager.list_history(50)})
        elif path == "/api/handoff/record":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            # The UI historically sent `name=`; accept both spellings.
            fname = (params.get("filename") or params.get("name") or ["current.json"])[0]
            # get_record_by_name joins this onto the archive dir, so a name with
            # a separator or leading dot could read any *.json on disk.
            if "/" in fname or "\\" in fname or fname.startswith(".") or "\x00" in fname:
                self._serve_json({"error": "Invalid handoff record name"}, status=400)
                return
            rec = handoff_manager.get_record_by_name(fname)
            self._serve_json({"record": rec})
        elif path in ("/api/metrics/tokens", "/api/tokens"):
            self._serve_json(orchestrator.swarm.token_tracker.get_metrics())
        elif path == "/api/router/history":
            self._serve_json({"history": orchestrator.router.get_routing_history(50)})
        elif path == "/api/git":
            self._serve_json(self._get_git_info())
        elif path == "/api/worktrees":
            records = orchestrator.worktrees.status()
            if isinstance(records, list):
                data = [r.to_dict() for r in records]
            elif records:
                data = [records.to_dict()]
            else:
                data = []
            self._serve_json({"worktrees": data})
        elif path == "/api/worktrees/diff":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            task_id = params.get("task_id", [None])[0]
            if not task_id:
                self._serve_json({"error": "Missing task_id query parameter"}, status=400)
            else:
                try:
                    diff_data = orchestrator.worktrees.diff(task_id)
                    self._serve_json(diff_data)
                except ValueError as exc:
                    self._serve_json({"error": str(exc)}, status=404)
                except Exception as exc:
                    self._serve_json({"error": str(exc)}, status=500)
        elif path == "/api/token":
            # Loopback session-auth handshake. Not a provider credential, so it
            # must bypass the response redactor or the UI can never authenticate.
            self._serve_json({"token": get_or_create_auth_token()}, redact=False)

        # ── Phase 12-14: Provider Management API ──────────────────────────
        elif path == "/api/providers":
            providers = []
            analytics = get_analytics_engine()
            for p in registry.list_providers():
                accts = registry.account_registry.list_accounts(p.id)
                h = p.health()
                p_type = "IDE" if p.id == "antigravity" else "AGENT"
                p_metrics = analytics.get_metrics(provider_id=p.id)
                providers.append({
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "type": p_type,
                    "enabled": p.enabled,
                    "healthy": h.get("healthy", False),
                    "status": "online" if h.get("healthy", False) else "offline",
                    "failure_rate": p_metrics.failure_rate,
                    "models": p.models,
                    "capabilities": sorted(c.value for c in p.capabilities),
                    "account_count": len(accts),
                    "accounts": [a.to_dict() for a in accts],
                    "last_health_check": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            # Also include direct API-only providers
            for ai_prov in registry.list_ai_providers():
                if not any(pp["id"] == ai_prov.provider_id for pp in providers):
                    accts = registry.account_registry.list_accounts(ai_prov.provider_id)
                    primary_acct = accts[0] if accts else None
                    ok, reason = ai_prov.health(primary_acct)
                    ai_metrics = analytics.get_metrics(provider_id=ai_prov.provider_id)
                    is_unconfig = "Missing credential" in reason or "AUTH_ERROR" in reason or not accts
                    status_label = "online" if ok else ("not_configured" if is_unconfig else "offline")
                    providers.append({
                        "id": ai_prov.provider_id,
                        "name": ai_prov.display_name,
                        "description": f"{ai_prov.display_name} API Provider",
                        "type": ai_prov.provider_type.value.upper(),
                        "enabled": True,
                        "healthy": ok,
                        "status": status_label,
                        "health_reason": reason,
                        "failure_rate": ai_metrics.failure_rate,
                        "models": [m.model_id for m in ai_prov.list_models(primary_acct)],
                        "capabilities": sorted(c.value for c in ai_prov.capabilities()),
                        "account_count": len(accts),
                        "accounts": [a.to_dict() for a in accts],
                        "last_health_check": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    })
            self._serve_json({"providers": providers, "count": len(providers)})
        elif path.startswith("/api/providers/") and not any(
            path.endswith(x) for x in ("/health", "/discover", "/enable", "/disable")
        ):
            p_id = path.split("/api/providers/")[1].split("/")[0]
            provider = registry.get_provider(p_id)
            ai_prov = registry.get_ai_provider(p_id)
            analytics = get_analytics_engine()
            if provider:
                h = provider.health()
                accts = registry.account_registry.list_accounts(p_id)
                p_metrics = analytics.get_metrics(provider_id=p_id)
                p_type = "IDE" if p_id == "antigravity" else "AGENT"
                self._serve_json({
                    "id": provider.id,
                    "name": provider.name,
                    "description": provider.description,
                    "type": p_type,
                    "enabled": provider.enabled,
                    "healthy": h.get("healthy", False),
                    "status": "online" if h.get("healthy", False) else "offline",
                    "failure_rate": p_metrics.failure_rate,
                    "adapters": h.get("adapters", {}),
                    "models": provider.models,
                    "capabilities": sorted(c.value for c in provider.capabilities),
                    "accounts": [a.to_dict() for a in accts],
                    "last_health_check": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            elif ai_prov:
                accts = registry.account_registry.list_accounts(p_id)
                primary_acct = accts[0] if accts else None
                ok, reason = ai_prov.health(primary_acct)
                ai_metrics = analytics.get_metrics(provider_id=p_id)
                is_unconfig = "Missing credential" in reason or "AUTH_ERROR" in reason or not accts
                status_label = "online" if ok else ("not_configured" if is_unconfig else "offline")
                self._serve_json({
                    "id": ai_prov.provider_id,
                    "name": ai_prov.display_name,
                    "description": f"{ai_prov.display_name} API Provider",
                    "type": ai_prov.provider_type.value.upper(),
                    "enabled": True,
                    "healthy": ok,
                    "status": status_label,
                    "health_reason": reason,
                    "failure_rate": ai_metrics.failure_rate,
                    "models": [m.model_id for m in ai_prov.list_models(primary_acct)],
                    "capabilities": sorted(c.value for c in ai_prov.capabilities()),
                    "accounts": [a.to_dict() for a in accts],
                    "last_health_check": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            else:
                self._serve_json({"error": f"Provider '{p_id}' not found"}, status=404)

        # ── Phase 22 Part 8: Granular account metrics API ────────────────
        # Eight explicit, independently-computed metrics, each derived from a
        # single orthogonal state field (never a conflated one). Registered
        # via string equality BEFORE the "/api/accounts/{id}" prefix route so
        # "metrics" is not mistaken for an account id.
        elif path == "/api/accounts/metrics":
            all_accounts = registry.account_registry.list_accounts()
            metrics = self._compute_account_metrics(all_accounts)
            self._serve_json({"metrics": metrics, "count": metrics["registered"]})

        # ── Phase 12-14: Account Management API ───────────────────────────
        elif path == "/api/accounts":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            provider_filter = params.get("provider", [None])[0]
            accts = registry.account_registry.list_accounts(provider_filter)
            u_tracker = get_usage_tracker()
            analytics = get_analytics_engine()
            res_accts = []
            for a in accts:
                d = a.to_dict()
                u = u_tracker.get_summary(account_id=a.id)
                d["requests"] = u["requests"]
                d["tokens"] = u["total_tokens"]
                d["estimated_cost"] = u["estimated_cost_usd"]
                d["failure_rate"] = analytics.get_metrics(account_id=a.id).failure_rate
                res_accts.append(d)
            self._serve_json({
                "accounts": res_accts,
                "count": len(res_accts),
            })
        elif path.startswith("/api/accounts/") and not any(
            path.endswith(x) for x in ("/health", "/enable", "/disable")
        ):
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            account = registry.account_registry.get_account(a_id)
            if account:
                d = account.to_dict()
                u = get_usage_tracker().get_summary(account_id=a_id)
                d["requests"] = u["requests"]
                d["tokens"] = u["total_tokens"]
                d["estimated_cost"] = u["estimated_cost_usd"]
                d["failure_rate"] = get_analytics_engine().get_metrics(account_id=a_id).failure_rate
                self._serve_json(d)
            else:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)

        # ── Phase 12-14: Model Registry API ───────────────────────────────
        elif path == "/api/models":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0]
            cap = params.get("capability", [None])[0]
            models = registry.model_registry.list_models(
                provider_id=prov,
                capability=cap,
            )
            model_dicts = []
            for m in models:
                md = m.to_dict()
                md["speed"] = md.get("latency_tier", "medium")
                md["availability"] = "available" if m.enabled else "unavailable"
                md["health"] = True
                model_dicts.append(md)
            self._serve_json({
                "models": model_dicts,
                "count": len(model_dicts),
            })

        # ── Phase 12-14: Job History API ──────────────────────────────────
        elif path == "/api/jobs":
            jobs = job_manager.list_jobs()
            self._serve_json({
                "jobs": [j.to_dict() for j in jobs],
                "count": len(jobs),
            })
        elif path.startswith("/api/jobs/"):
            j_id = path.split("/api/jobs/")[1].split("/")[0]
            job = job_manager.get_job(j_id)
            if job:
                self._serve_json(job.to_dict())
            else:
                self._serve_json({"error": f"Job '{j_id}' not found"}, status=404)

        # ── Phase 13: Routing History & Inspect API ───────────────────────
        elif path == "/api/routing/history":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            lim = _as_int(params.get("limit", [50])[0], "limit", 50)
            jid = params.get("job_id", [None])[0]
            router = SmartRouter(registry)
            history = router.get_routing_history(limit=lim, job_id=jid)
            self._serve_json({"history": history, "count": len(history)})
        elif path == "/api/routing/inspect":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            jid = params.get("job_id", [None])[0]
            if not jid:
                self._serve_json({"error": "Missing job_id query parameter"}, status=400)
            else:
                router = SmartRouter(registry)
                dec = router.get_decision(jid)
                if dec:
                    self._serve_json({"decision": dec.to_dict() if hasattr(dec, "to_dict") else dec})
                else:
                    j = job_manager.get_job(jid)
                    if j and j.metadata.get("routing_decision"):
                        self._serve_json({"decision": j.metadata["routing_decision"]})
                    else:
                        recs = router.get_routing_history(limit=1, job_id=jid)
                        if recs:
                            self._serve_json({"decision": recs[0]})
                        else:
                            self._serve_json({"error": f"No routing record found for job '{jid}'"}, status=404)

        # ── Phase 14: Usage & Cost Analytics API ──────────────────────────
        elif path == "/api/usage":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0]
            acct = params.get("account", [None])[0]
            mod = params.get("model", [None])[0]
            day = params.get("day", [None])[0]
            month = params.get("month", [None])[0]
            grp = params.get("group_by", [None])[0]
            u_tracker = get_usage_tracker()
            if grp:
                breakdown = u_tracker.get_breakdown(group_by=grp)
                self._serve_json({"group_by": grp, "breakdown": breakdown})
            else:
                summary = u_tracker.get_summary(provider_id=prov, account_id=acct, model_id=mod, day=day, month=month)
                summary["by_provider"] = u_tracker.get_breakdown(group_by="provider")
                self._serve_json(summary)
        elif path == "/api/cost":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0]
            acct = params.get("account", [None])[0]
            mod = params.get("model", [None])[0]
            day = params.get("day", [None])[0]
            month = params.get("month", [None])[0]
            summary = get_usage_tracker().get_summary(provider_id=prov, account_id=acct, model_id=mod, day=day, month=month)
            pricing = get_cost_tracker().list_pricing()
            today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            today_sum = get_usage_tracker().get_summary(day=today_str)
            self._serve_json({
                "summary": summary,
                "total_estimated_cost_usd": summary.get("estimated_cost_usd", 0.0),
                "today_estimated_cost_usd": today_sum.get("estimated_cost_usd", 0.0),
                "pricing": pricing,
                "pricing_models": pricing,
            })
        elif path == "/api/quotas":
            qm = get_quota_manager()
            rules = qm.list_quotas()
            augmented = []
            for r in rules:
                st = qm.check_target_quota(r["target_type"], r["target_id"])
                augmented.append({"rule": r, "status": st.to_dict()})
            alerts = qm.get_active_alerts()
            self._serve_json({"quotas": augmented, "alerts": alerts, "count": len(augmented)})
        elif path == "/api/analytics":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            prov = params.get("provider", [None])[0]
            acct = params.get("account", [None])[0]
            mod = params.get("model", [None])[0]
            metrics = get_analytics_engine().get_metrics(provider_id=prov, account_id=acct, model_id=mod)
            res_dict = metrics.to_dict()
            res_dict["latency"] = {
                "p50": res_dict.get("p50_latency", 0.0),
                "p95": res_dict.get("p95_latency", 0.0),
                "p99": res_dict.get("p99_latency", 0.0),
            }
            self._serve_json(res_dict)

        # ── Phase 13: Routing Status API ──────────────────────────────
        elif path == "/api/routing/status":
            router = SmartRouter(registry)
            all_accts = registry.account_registry.list_accounts()
            status_list = []
            for a in all_accts:
                status_list.append({
                    "account_id": a.id,
                    "provider_id": a.provider_id,
                    "status": a.status.value,
                    "enabled": a.enabled,
                    "priority": a.priority,
                    "available": a.is_available(),
                    "cooldown_active": bool(a.cooldown_until and time.time() < a.cooldown_until),
                    "failure_count": a.failure_count,
                })
            self._serve_json({
                "accounts": status_list,
                "total_available": sum(1 for s in status_list if s["available"]),
                "total_accounts": len(status_list),
            })

        # ── Phase 15: Universal MCP, Steering, Knowledge, Tools & Resources ──
        elif path == "/api/mcp":
            from providers.mcp.discovery import MCPDiscoveryEngine
            from providers.mcp.tool_catalog import MCPToolCatalog
            from providers.registry.mcp_registry import get_mcp_registry
            reg = get_mcp_registry()
            servers = reg.list_servers()
            if not servers:
                engine = MCPDiscoveryEngine()
                catalog = MCPToolCatalog()
                for disc in engine.discover():
                    s = disc.to_mcp_server()
                    catalog.populate_from_server(s.id)
                    s.tools = catalog.list_tools(server_id=s.id)
                    reg.register_server(s)
                servers = reg.list_servers()
            self._serve_json({"servers": [s.to_dict() for s in servers], "count": len(servers)})
        elif path.startswith("/api/mcp/"):
            mcp_id = path.split("/api/mcp/")[1].split("/")[0]
            from providers.registry.mcp_registry import get_mcp_registry
            server = get_mcp_registry().get_server(mcp_id)
            if server:
                self._serve_json(server.to_dict())
            else:
                self._serve_json({"error": f"MCP server '{mcp_id}' not found"}, status=404)
        elif path == "/api/tools":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            q = params.get("q", [None])[0]
            from providers.mcp.discovery import MCPDiscoveryEngine
            from providers.mcp.tool_catalog import MCPToolCatalog
            catalog = MCPToolCatalog()
            engine = MCPDiscoveryEngine()
            for disc in engine.discover():
                catalog.populate_from_server(disc.server_id)
            if q:
                ranked = catalog.search_tools(q, limit=20)
                self._serve_json({"tools": [{"tool": t.to_dict(), "score": round(s, 2)} for t, s in ranked]})
            else:
                tools = catalog.list_tools()
                self._serve_json({"tools": [t.to_dict() for t in tools], "count": len(tools)})
        elif path == "/api/steering":
            from brain.knowledge.steering_registry import SteeringRegistry
            reg = SteeringRegistry()
            docs = reg.discover()
            conflicts = reg.get_conflicts()
            self._serve_json({
                "steering": [d.to_dict() for d in docs],
                "conflicts": [c.to_dict() for c in conflicts],
                "count": len(docs)
            })
        elif path == "/api/knowledge":
            query = self.path.split("?")[1] if "?" in self.path else ""
            params = urllib.parse.parse_qs(query)
            q = params.get("q", [None])[0]
            from brain.knowledge.document_registry import DocumentRegistry
            reg = DocumentRegistry()
            docs = reg.discover()
            if q:
                from brain.knowledge.relevance import KnowledgeRelevanceEngine
                rel = KnowledgeRelevanceEngine().rank_documents(task=q, documents=docs, limit=20)
                self._serve_json(rel.to_dict())
            else:
                self._serve_json({"documents": [d.to_dict() for d in docs], "count": len(docs)})
        elif path == "/api/resources":
            from brain.context.context_builder import ContextBuilder
            from brain.resources.resource_graph import ResourceGraphBuilder
            builder = ContextBuilder()
            sel = builder.selector
            graph = ResourceGraphBuilder.build_graph(
                None, sel.mcp_registry, sel.steering_registry,
                sel.document_registry, sel.cli_registry, sel.repo_registry
            )
            self._serve_json(graph.to_graph_data())

        else:
            self.send_response(404)
            self._apply_security_headers()
            self.end_headers()

    def do_POST(self) -> None:
        self._dispatch(self._handle_POST)

    def _handle_POST(self) -> None:
        if not self._check_origin():
            return

        path = self.path.split("?")[0]
        payload = self._read_json_body()
        if payload is None:
            return

        # 1. Unauthenticated endpoints
        if path == "/api/route":
            router = SmartRouter(registry)
            dec = router.route(
                task_text=payload.get("instruction", ""),
                preferred_agent=payload.get("agent"),
                preferred_model=payload.get("model"),
            )
            self._serve_json({
                "agent_id": dec.agent_id,
                "account_id": dec.account_id,
                "model": dec.model,
                "complexity": dec.complexity.value,
                "reason": dec.reason,
                "fallback_agent_id": dec.fallback_agent_id,
                "candidates": dec.candidates,
            })
            return

        elif path == "/api/context/preview":
            # The preview includes retrieved memory contents; require a session.
            if not self._verify_auth(path):
                return
            task = payload.get("task", "") or payload.get("instruction", "")
            from brain.context.context_builder import ContextBuilder
            builder = ContextBuilder()
            ctx = builder.preview_context(task)
            self._serve_json(ctx.to_dict())
            return

        # 2. Mutating endpoints requiring Bearer token authentication
        mutating_paths = {
            "/api/dispatch",
            "/api/task/dispatch",
            "/api/execute",
            "/api/task/execute",
            "/api/continue",
            "/api/tasks/cancel",
            "/api/task/cancel",
            "/api/tasks/reconcile",
            "/api/memory/add",
            # Returns stored memory contents, so it needs a session like GET /api/memory.
            "/api/memory/search",
            "/api/worktrees/apply",
            "/api/worktrees/approve",
            "/api/worktrees/reject",
            "/api/worktrees/cleanup",
            "/api/worktrees/recover",
            "/api/providers",
            "/api/models/discover",
            "/api/quotas",
            "/api/quotas/reset",
            "/api/jobs",
            "/api/accounts",
        }
        if path in mutating_paths:
            if not self._verify_auth(path):
                return

        # 3. Rate limiting for task execution endpoints
        if path in ("/api/dispatch", "/api/task/dispatch", "/api/continue", "/api/execute", "/api/task/execute"):
            client_ip = self.client_address[0] if hasattr(self, "client_address") else "127.0.0.1"
            if not execution_rate_limiter.is_allowed(client_ip):
                self._serve_json(
                    {
                        "error": "Too Many Requests",
                        "message": "Execution rate limit exceeded (30 req/min). Try again later.",
                    },
                    status=429,
                )
                return

        # 4. Confirmation requirement for destructive actions
        destructive_paths = {
            "/api/worktrees/apply",
            "/api/worktrees/approve",
            "/api/worktrees/reject",
            "/api/worktrees/cleanup",
        }
        if path in destructive_paths:
            if payload.get("confirm") is not True:
                self._serve_json(
                    {
                        "error": "Confirmation required",
                        "message": "Explicit confirmation ('confirm': true) required for destructive worktree operations",
                    },
                    status=400,
                )
                return

        # 5. Endpoint dispatch handlers
        if path in ("/api/dispatch", "/api/task/dispatch"):
            _invalidate_system_status_cache()
            instruction = payload.get("instruction") or payload.get("title") or payload.get("prompt") or "Untitled Task"
            agent = payload.get("agent") or payload.get("target_agent") or payload.get("assigned_to") or None
            task = orchestrator.plan_and_dispatch(
                instruction=instruction,
                preferred_agent=agent,
                preferred_model=payload.get("model") or None,
                files=payload.get("files") or None,
            )
            auto_execute = payload.get("auto_execute", True)
            if auto_execute:
                threading.Thread(target=orchestrator.execute_next, daemon=True).start()
            self._serve_json({
                "status": "created",
                "task": task.to_dict(),
                "task_id": task.task_id,
                "executing": auto_execute,
            })
        elif path in ("/api/execute", "/api/task/execute"):
            _invalidate_system_status_cache()
            task_id = payload.get("task_id")
            if task_id:
                # Run the task the operator asked for, not whatever is next in
                # the queue. execute_task still enforces the approval gate.
                task = task_manager.get_task(str(task_id))
                if task is None:
                    self._serve_json({"error": f"Task '{task_id}' not found"}, status=404)
                    return
                if task.status != TaskStatus.READY:
                    self._serve_json(
                        {"error": f"Task '{task_id}' is {task.status.value}, not READY"},
                        status=409,
                    )
                    return
                target = functools.partial(orchestrator.execute_task_now, task)
            else:
                target = orchestrator.execute_next
            threading.Thread(target=target, daemon=True).start()
            self._serve_json({
                "status": "executing",
                "task_id": task_id,
                "message": "Triggered swarm execution worker",
            })
        elif path in ("/api/tasks/cancel", "/api/task/cancel"):
            task_id = payload.get("task_id")
            if not task_id:
                self._serve_json({"error": "Missing task_id"}, status=400)
                return
            cancelled = task_manager.update_status(
                task_id,
                TaskStatus.CANCELLED,
                is_terminal=True,
                terminal_reason="USER_CANCELLED",
            )
            if cancelled:
                self._serve_json({"status": "cancelled", "task": cancelled.to_dict()})
            else:
                self._serve_json({"error": f"Task '{task_id}' not found"}, status=404)
        elif path == "/api/tasks/reconcile":
            active_ids = orchestrator.swarm.get_active_task_ids()
            stats = task_manager.reconcile_runtime_state(active_ids)
            self._serve_json(stats)
        elif path == "/api/continue":
            ctx = orchestrator.build_continue_context()
            if getattr(ctx, "is_terminal", False):
                self._serve_json({"status": "terminal", "reason": ctx.terminal_reason, "context": ctx.to_dict()})
            else:
                threading.Thread(target=orchestrator.continue_work, daemon=True).start()
                self._serve_json({"status": "continued", "context": ctx.to_dict()})
        elif path == "/api/memory/search":
            query_text = payload.get("query", "")
            scope_str = payload.get("scope")
            scope_enum = None
            if scope_str:
                try:
                    scope_enum = MemoryScope(str(scope_str).upper())
                except Exception:
                    pass
            retriever = MemoryRetriever(memory_store)
            results = retriever.retrieve_context(
                query=query_text,
                scope=scope_enum,
                max_items=10,
                max_bytes=4096,
            )
            self._serve_json({
                "query": query_text,
                "count": len(results),
                "memories": [m.to_dict() for m in results],
            })
        elif path == "/api/memory/add":
            content = payload.get("content", "").strip()
            if not content:
                self._serve_json({"error": "Memory content cannot be empty"}, status=400)
                return
            scope_str = str(payload.get("scope") or "PROJECT").upper()
            try:
                scope = MemoryScope(scope_str)
            except ValueError:
                raise BadRequest(
                    f"Unknown memory scope {scope_str[:40]!r}; expected one of "
                    + ", ".join(m.value for m in MemoryScope)
                ) from None
            entry = memory_store.add(
                content=content,
                scope=scope,
                source_agent=payload.get("source_agent", "user-mission-control"),
                importance=_as_int(payload.get("importance"), "importance", 3),
                tags=payload.get("tags", []),
            )
            self._serve_json({"status": "added", "memory": entry.to_dict()})
        elif path in ("/api/worktrees/apply", "/api/worktrees/approve"):
            task_id = payload.get("task_id")
            if not task_id:
                self._serve_json({"error": "Missing task_id"}, status=400)
                return
            if orchestrator.worktrees.get(task_id) is None:
                self._serve_json({"error": f"No worktree record for task '{task_id}'"}, status=404)
                return
            try:
                approver = payload.get("approver", "mission_control")
                result = orchestrator.worktrees.apply(
                    task_id=task_id, approver=approver, confirm=True
                )
                event_bus.emit(
                    Event(
                        event_type=EventType.DIFF_APPROVED,
                        task_id=task_id,
                        metadata={"approver": approver},
                    )
                )
                event_bus.emit(
                    Event(
                        event_type=EventType.MERGE_APPLIED,
                        task_id=task_id,
                        metadata={"result": result},
                    )
                )
                self._serve_json({"status": "applied", "task_id": task_id, "result": result})
            except RuntimeError as exc:
                self._serve_json(
                    {"error": "Conflict or Dirty Canonical Tree", "message": str(exc)},
                    status=409,
                )
            except Exception as exc:
                self._serve_json({"error": str(exc)}, status=500)
        elif path == "/api/worktrees/reject":
            task_id = payload.get("task_id")
            if not task_id:
                self._serve_json({"error": "Missing task_id"}, status=400)
                return
            if orchestrator.worktrees.get(task_id) is None:
                self._serve_json({"error": f"No worktree record for task '{task_id}'"}, status=404)
                return
            try:
                reason = payload.get("reason", "Rejected via Mission Control")
                result = orchestrator.worktrees.reject(task_id=task_id, reason=reason, confirm=True)
                event_bus.emit(
                    Event(
                        event_type=EventType.DIFF_REJECTED,
                        task_id=task_id,
                        metadata={"reason": reason},
                    )
                )
                event_bus.emit(
                    Event(
                        event_type=EventType.WORKTREE_DESTROYED,
                        task_id=task_id,
                        metadata={"action": "reject", "result": result},
                    )
                )
                self._serve_json({"status": "rejected", "task_id": task_id, "result": result})
            except Exception as exc:
                self._serve_json({"error": str(exc)}, status=500)
        elif path == "/api/worktrees/cleanup":
            task_id = payload.get("task_id")
            # Validate before touching anything (400, not a dropped connection).
            max_age = _as_int(payload.get("max_age_hours"), "max_age_hours", 24)
            try:
                if task_id:
                    if orchestrator.worktrees.get(task_id) is None:
                        self._serve_json({"error": f"No worktree record for task '{task_id}'"}, status=404)
                        return
                    # WorktreeManager.cleanup(task_id, force, delete_branch);
                    # `remove` is an alias that accepts no delete_branch/confirm.
                    result = orchestrator.worktrees.cleanup(
                        task_id,
                        force=True,
                        delete_branch=bool(payload.get("delete_branch", True)),
                    )
                    event_bus.emit(
                        Event(
                            event_type=EventType.WORKTREE_DESTROYED,
                            task_id=task_id,
                            metadata={"action": "cleanup_single"},
                        )
                    )
                    self._serve_json({"status": "cleaned", "task_id": task_id, "result": result})
                else:
                    # WorktreeManager has no age-based batch cleanup, so select
                    # here: only finished/abandoned sandboxes older than
                    # max_age_hours. ACTIVE/CREATED/PENDING_REVIEW/APPROVED hold
                    # unreviewed work and are never swept in bulk.
                    cleaned = _cleanup_stale_worktrees(orchestrator.worktrees, max_age)
                    for item in cleaned:
                        event_bus.emit(
                            Event(
                                event_type=EventType.WORKTREE_DESTROYED,
                                task_id=item.get("task_id"),
                                metadata={"action": "cleanup_batch"},
                            )
                        )
                    self._serve_json({"status": "cleaned", "cleaned": cleaned})
            except Exception as exc:
                self._serve_json({"error": str(exc)}, status=500)
        elif path == "/api/worktrees/recover":
            task_id = payload.get("task_id")
            try:
                # WorktreeManager.recover() takes no arguments: it scans every
                # record for orphans. task_id (optional) narrows the response.
                recovered = orchestrator.worktrees.recover()
                body: dict[str, Any] = {
                    "status": "recovered",
                    "recovered": [r.to_dict() for r in recovered],
                    "count": len(recovered),
                }
                if task_id:
                    record = orchestrator.worktrees.get(task_id)
                    if record is None:
                        self._serve_json({"error": f"No worktree record for task '{task_id}'"}, status=404)
                        return
                    body["worktree"] = record.to_dict()
                self._serve_json(body)
            except Exception as exc:
                self._serve_json({"error": str(exc)}, status=500)

        # ── Phase 12-14: Account Creation ──────────────────────────────
        elif path == "/api/accounts":
            if not self._verify_auth(path):
                return
            name = (payload.get("name") or payload.get("account_id") or "").strip()
            provider_id = (payload.get("provider") or payload.get("provider_id") or "").strip()
            if not name or not provider_id:
                self._serve_json({"error": "Missing required fields: name/account_id, provider/provider_id"}, status=400)
                return
            # Validate numeric fields before any credential is stored.
            priority = _as_int(payload.get("priority"), "priority", 10)
            concurrency_limit = _as_int(payload.get("concurrency_limit"), "concurrency_limit", 2)
            account_id = payload.get("account_id") or (f"{provider_id}-{name}" if not name.startswith(provider_id) else name)
            if registry.account_registry.get_account(account_id):
                self._serve_json({"error": f"Account '{account_id}' already exists"}, status=409)
                return
            auth_type_str = payload.get("auth_type") or payload.get("auth_method") or "api_key"
            try:
                auth_type = AuthenticationType(auth_type_str)
            except Exception:
                auth_type = AuthenticationType.API_KEY
            cred_ref = payload.get("credential_reference", "")
            if payload.get("api_key"):
                cred_ref = f"secret://mission-control/{provider_id}/{account_id}/api_key"
                get_credential_manager().store_credential(cred_ref, payload["api_key"])
            models_list = payload.get("models", [])
            if isinstance(models_list, str):
                models_list = [m.strip() for m in models_list.split(",") if m.strip()]
            new_account = Account(
                id=account_id,
                provider_id=provider_id,
                account_name=name,
                display_name=payload.get("display_name", name),
                account_type=payload.get("account_type", "api"),
                authentication_type=auth_type,
                credential_reference=cred_ref,
                status=AccountStatus.ONLINE if (cred_ref or auth_type in (AuthenticationType.UNAUTHENTICATED, AuthenticationType.LOCAL, AuthenticationType.OAUTH)) else AccountStatus.NOT_CONFIGURED,
                enabled=True,
                priority=priority,
                models=models_list,
                capabilities=payload.get("capabilities", []),
                concurrency_limit=concurrency_limit,
            )
            registry.account_registry.register_account(new_account)
            try:
                acct_data = {
                    "account_id": name,
                    "agent_id": account_id,
                    "display_name": payload.get("display_name", name),
                    "credential_reference": cred_ref,
                    "authentication_type": auth_type.value if hasattr(auth_type, "value") else str(auth_type),
                    "enabled": True,
                    "priority": new_account.priority,
                    "models": models_list,
                    "capabilities": payload.get("capabilities", []),
                    "concurrency_limit": new_account.concurrency_limit,
                }
                add_account_config(provider_id, account_id, acct_data)
                ensure_registry_synced(force=True)
            except Exception as persist_err:
                logger.warning(f"Failed to persist account to config/providers.json: {persist_err}")
            self._serve_json({"status": "created", "account": new_account.to_dict()}, status=201)

        # ── Phase 12-14: Account Health Check ──────────────────────────
        elif path.startswith("/api/accounts/") and path.endswith("/health"):
            if not self._verify_auth(path):
                return
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            account = registry.account_registry.get_account(a_id)
            if not account:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)
                return
            pool = registry.account_registry.get_pool(account.provider_id)
            adapter = registry.get_adapter(a_id)
            if adapter:
                healthy, reason = adapter.health()
                if healthy:
                    pool.record_success(a_id)
                else:
                    pool.record_failure(a_id, error=reason)
                account.last_health_check = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self._serve_json({
                    "account_id": a_id,
                    "healthy": healthy,
                    "reason": reason,
                    "status": account.status.value,
                })
            else:
                cred_ok = False
                if account.credential_reference:
                    cred_ok = get_credential_manager().exists(account.credential_reference)
                account.last_health_check = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if cred_ok:
                    account.status = AccountStatus.ONLINE
                    self._serve_json({
                        "account_id": a_id,
                        "healthy": True,
                        "reason": "Credential reference validated",
                        "status": account.status.value,
                    })
                else:
                    account.status = AccountStatus.CONFIG_ERROR if not account.credential_reference else AccountStatus.AUTH_ERROR
                    self._serve_json({
                        "account_id": a_id,
                        "healthy": False,
                        "reason": "No credential reference" if not account.credential_reference else "Credential not found",
                        "status": account.status.value,
                    })

        # ── Phase 12-14: Account Enable/Disable ────────────────────────
        elif path.startswith("/api/accounts/") and path.endswith("/enable"):
            if not self._verify_auth(path):
                return
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            if registry.account_registry.enable_account(a_id):
                self._serve_json({"status": "enabled", "account_id": a_id})
            else:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)
        elif path.startswith("/api/accounts/") and path.endswith("/disable"):
            if not self._verify_auth(path):
                return
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            if registry.account_registry.disable_account(a_id):
                self._serve_json({"status": "disabled", "account_id": a_id})
            else:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)

        # ── Phase 12: Provider Creation & Management ───────────────────
        elif path == "/api/providers":
            if not self._verify_auth(path):
                return
            p_id = payload.get("id")
            p_id = p_id.strip() if isinstance(p_id, str) else ""
            if not _PROVIDER_ID_RE.match(p_id):
                raise BadRequest("'id' is required: 1-64 chars of letters, digits, '.', '_' or '-'")
            name = str(payload.get("name") or "").strip() or p_id.capitalize()
            p_type = payload.get("type", "api").lower()
            base_url = payload.get("base_url", "https://api.openai.com/v1")
            models = payload.get("models", [])
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            default_model = payload.get("default_model", models[0] if models else "default")
            cred_ref = payload.get("credential_reference", "")
            if payload.get("api_key"):
                cred_ref = f"secret://mission-control/{p_id}/provider_api_key"
                get_credential_manager().store_credential(cred_ref, payload["api_key"])

            if p_type in ("ollama", "local", "local_model"):
                ai_prov = OllamaProvider(provider_id=p_id, base_url=base_url, default_model=default_model)
            elif p_type == "gemini":
                ai_prov = GeminiProvider(provider_id=p_id, default_model=default_model)
            elif p_type == "anthropic":
                ai_prov = AnthropicProvider(provider_id=p_id, default_model=default_model)
            else:
                ai_prov = OpenAICompatibleProvider(provider_id=p_id, base_url=base_url, credential_reference=cred_ref, default_model=default_model)

            registry.register_ai_provider(ai_prov)

            if cred_ref or payload.get("api_key"):
                acct_id = f"{p_id}-primary"
                if not registry.account_registry.get_account(acct_id):
                    def_acct = Account(
                        id=acct_id,
                        provider_id=p_id,
                        account_name=f"{name} Primary",
                        display_name=f"{name} Primary",
                        account_type="api",
                        authentication_type=AuthenticationType.API_KEY,
                        credential_reference=cred_ref,
                        status=AccountStatus.ONLINE,
                        enabled=True,
                        priority=10,
                        models=models,
                        capabilities=payload.get("capabilities", ["chat"]),
                    )
                    registry.account_registry.register_account(def_acct)

            self._serve_json({"status": "created", "provider_id": p_id}, status=201)

        elif path.startswith("/api/providers/") and path.endswith("/health"):
            if not self._verify_auth(path):
                return
            p_id = path.split("/api/providers/")[1].split("/")[0]
            provider = registry.get_provider(p_id)
            ai_prov = registry.get_ai_provider(p_id)
            if provider:
                h = provider.health()
                self._serve_json({"provider_id": p_id, "healthy": h.get("healthy", False), "details": h})
            elif ai_prov:
                accts = registry.account_registry.list_accounts(p_id)
                primary_acct = accts[0] if accts else None
                ok, reason = ai_prov.health(primary_acct)
                self._serve_json({"provider_id": p_id, "healthy": ok, "reason": reason})
            else:
                self._serve_json({"error": f"Provider '{p_id}' not found"}, status=404)

        elif path.startswith("/api/providers/") and path.endswith("/discover"):
            if not self._verify_auth(path):
                return
            p_id = path.split("/api/providers/")[1].split("/")[0]
            ai_prov = registry.get_ai_provider(p_id)
            if ai_prov:
                discovered = ai_prov.list_models()
                for m in discovered:
                    registry.model_registry.register_model(m)
                self._serve_json({"provider_id": p_id, "count": len(discovered), "models": [m.to_dict() for m in discovered]})
            else:
                self._serve_json({"error": f"Provider '{p_id}' not found or does not support model discovery"}, status=404)

        elif path.startswith("/api/providers/") and (path.endswith("/enable") or path.endswith("/disable")):
            if not self._verify_auth(path):
                return
            parts = path.split("/api/providers/")[1].split("/")
            p_id = parts[0]
            enable = (parts[1] == "enable")
            provider = registry.get_provider(p_id)
            if provider:
                provider.enabled = enable
                self._serve_json({"status": "updated", "provider_id": p_id, "enabled": enable})
            else:
                ai_prov = registry.get_ai_provider(p_id)
                if ai_prov:
                    self._serve_json({"status": "updated", "provider_id": p_id, "enabled": enable})
                else:
                    self._serve_json({"error": f"Provider '{p_id}' not found"}, status=404)

        # ── Phase 12: Model Discovery across all providers ─────────────
        elif path == "/api/models/discover":
            if not self._verify_auth(path):
                return
            discovered_all = []
            for ai_prov in registry.list_ai_providers():
                try:
                    for m in ai_prov.list_models():
                        registry.model_registry.register_model(m)
                        discovered_all.append(m.to_dict())
                except Exception:
                    pass
            self._serve_json({"status": "discovered", "count": len(discovered_all), "models": discovered_all})

        # ── Phase 14: Quotas Configuration ─────────────────────────────
        elif path == "/api/quotas":
            if not self._verify_auth(path):
                return
            target_type = payload.get("target_type", "").strip()
            target_id = payload.get("target_id", "").strip()
            if not target_type or not target_id:
                self._serve_json({"error": "target_type and target_id are required"}, status=400)
                return
            rule = get_quota_manager().set_quota(
                target_type=target_type,
                target_id=target_id,
                daily_requests=payload.get("daily_requests"),
                daily_tokens=payload.get("daily_tokens"),
                monthly_requests=payload.get("monthly_requests"),
                monthly_tokens=payload.get("monthly_tokens"),
                daily_cost=payload.get("daily_cost"),
                monthly_cost=payload.get("monthly_cost"),
                enabled=payload.get("enabled", True),
            )
            self._serve_json({"status": "saved", "rule": rule.to_dict()}, status=201)

        elif path == "/api/quotas/reset":
            if not self._verify_auth(path):
                return
            target_type = payload.get("target_type", "").strip()
            target_id = payload.get("target_id", "").strip()
            if not target_type or not target_id:
                self._serve_json({"error": "target_type and target_id are required"}, status=400)
                return
            removed = get_quota_manager().remove_quota(target_type=target_type, target_id=target_id)
            self._serve_json({"status": "reset", "removed": removed})

        # ── Phase 12-13: Job Submission with Multi-Factor SmartRouter ───
        elif path == "/api/jobs":
            if not self._verify_auth(path):
                return
            task_text = (payload.get("task") or "").strip()
            if not task_text:
                self._serve_json({"error": "Missing 'task' field"}, status=400)
                return
            prov = (payload.get("provider") or "").strip()
            acct = (payload.get("account") or "").strip()
            mdl = (payload.get("model") or "").strip()
            mode = payload.get("routing_mode", "balanced")
            streaming = bool(payload.get("streaming", False))
            failover_enabled = bool(payload.get("failover_enabled", True))

            decision = None
            if not prov or prov.lower() == "auto" or not acct or acct.lower() == "auto" or not mdl or mdl.lower() == "auto":
                router = SmartRouter(registry)
                pref_agent = prov if (prov and prov.lower() != "auto") else None
                pref_acct = acct if (acct and acct.lower() != "auto") else None
                pref_mdl = mdl if (mdl and mdl.lower() != "auto") else None
                try:
                    decision = router.route(
                        task_text=task_text,
                        preferred_agent=pref_agent,
                        preferred_account=pref_acct,
                        preferred_model=pref_mdl,
                        routing_mode=mode,
                        requires_streaming=streaming,
                    )
                    prov = decision.provider_id or decision.agent_id
                    # The decision's account_id is the adapter's short name
                    # ("account-3"); the account registry is keyed by agent id
                    # ("antigravity-account-3"). Hand JobManager a registry key.
                    acct = next(
                        (
                            cand for cand in (decision.agent_id, decision.account_id)
                            if cand and job_manager.accounts.get_account(cand) is not None
                        ),
                        decision.account_id,
                    )
                    mdl = decision.model
                except Exception as r_err:
                    self._serve_json({"error": f"SmartRouter failed: {r_err}"}, status=503)
                    return

            failover_chain = decision.failover_chain if (decision and failover_enabled) else None

            job = job_manager.submit_job(
                task=task_text,
                provider=prov,
                account=acct,
                model=mdl,
                metadata={
                    "routing_mode": mode,
                    "streaming": streaming,
                    "failover_enabled": failover_enabled,
                    "routing_decision": decision.to_dict() if decision else None,
                },
                failover_chain=failover_chain,
            )

            if decision:
                decision.job_id = job.id
                job.metadata["routing_decision"] = decision.to_dict()
                router = SmartRouter(registry)
                router.record_decision(decision)

            if payload.get("auto_execute", True):
                threading.Thread(
                    target=job_manager.execute_job,
                    args=(job,),
                    kwargs={"failover_chain": failover_chain},
                    daemon=True,
                ).start()

            resp = {
                "status": "submitted",
                "job": job.to_dict(),
            }
            if decision:
                resp["routing_decision"] = decision.to_dict()
                resp["explanation"] = decision.explain()
            self._serve_json(resp, status=200)

        # ── Phase 22 Part 9: Account Add Wizard (multi-step, real lifecycle) ──
        # Every step drives a real AccountLifecycleState transition and emits a
        # redacted, correlation-tagged event. All steps require the mutating
        # Bearer token. Secrets go straight to CredentialManager; only a
        # secret:// reference is ever retained or surfaced. Back / Cancel / Retry
        # are supported at every step; Cancel rolls back with zero orphans.
        elif path == "/api/wizard/start":
            if not self._verify_auth(path):
                return
            provider_id = (payload.get("provider_id") or payload.get("provider") or "").strip()
            raw_account_id = (payload.get("account_id") or payload.get("name") or "").strip()
            account_id = re.sub(r"[^a-zA-Z0-9_\-]+", "-", raw_account_id).strip("-").lower()
            account_id = re.sub(r"-+", "-", account_id)
            if not provider_id or not account_id:
                self._serve_json({"error": "provider_id and a valid account_id are required"}, status=400)
                return
            if registry.account_registry.get_account(account_id):
                # OmniRoute multi-account parity: auto-increment suffix if account already exists
                base_id = re.sub(r"-\d+$", "", account_id)
                n = 1
                while registry.account_registry.get_account(f"{base_id}-{n}"):
                    n += 1
                account_id = f"{base_id}-{n}"
            sess = wizard_manager.start(provider_id, account_id)
            self._serve_json({"status": "started", "wizard": sess.to_dict()}, status=201)

        elif path.startswith("/api/wizard/") and path.split("/")[-1] in (
            "select-auth", "configure", "authenticate", "validate",
            "register", "health", "complete", "back", "cancel", "retry",
            "launch-login", "check-auth",
        ):
            if not self._verify_auth(path):
                return
            action = path.split("/")[-1]
            wizard_id = (payload.get("wizard_id") or "").strip()
            sess = wizard_manager.get(wizard_id)
            if not sess:
                self._serve_json({"error": f"Wizard session '{wizard_id}' not found or already finished"}, status=404)
                return
            try:
                if action == "select-auth":
                    auth_m = (payload.get("auth_method") or payload.get("login_method") or "").strip()
                    wizard_manager.select_auth(sess, auth_m)
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "configure":
                    wizard_manager.configure(sess, dict(payload.get("config") or payload))
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "launch-login":
                    origin = f"http://{self.headers.get('Host', '127.0.0.1:3333')}"
                    if payload.get("email"):
                        sess.config["email"] = str(payload.get("email")).strip()
                    info = wizard_manager.launch_login(sess, redirect_origin=origin)
                    self._serve_json({"status": "ok", "login": info, "wizard": sess.to_dict()})
                elif action == "check-auth":
                    status_info = wizard_manager.check_auth_status(sess)
                    self._serve_json(
                        _redact_preserving_booleans({"status": "ok", "auth_status": status_info, "wizard": sess.to_dict()}),
                        redact=False,
                    )
                elif action == "authenticate":
                    wizard_manager.authenticate(sess)
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "validate":
                    wizard_manager.validate(sess, live=bool(payload.get("live", True)))
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "register":
                    wizard_manager.register(sess)
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "health":
                    result = wizard_manager.health_check(sess)
                    self._serve_json({"status": "ok", "health": result, "wizard": sess.to_dict()})
                elif action == "complete":
                    wizard_manager.complete(sess)
                    self._serve_json({"status": "complete", "wizard": sess.to_dict()})
                elif action == "back":
                    wizard_manager.back(sess)
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "retry":
                    # Retry re-clears the failure marker so the client can re-issue
                    # the failed step. The lifecycle already sits in a failure
                    # state from which CONFIGURING/AUTHENTICATING/VALIDATING is a
                    # legal recovery transition per the state machine.
                    sess.last_error = None
                    sess.failure_state = None
                    self._serve_json({"status": "ok", "wizard": sess.to_dict()})
                elif action == "cancel":
                    cleanup = wizard_manager.cancel(sess)
                    self._serve_json({"status": "cancelled", "cleanup": cleanup, "wizard": sess.to_dict()})
            except WizardError as werr:
                wizard_manager.fail(sess, werr)
                self._serve_json({
                    "error": str(werr),
                    "failure_state": werr.failure_state.value,
                    "wizard": sess.to_dict(),
                }, status=422)
            except Exception as exc:
                # Unexpected error: land in CONFIG_ERROR with an actionable message.
                werr = WizardError(f"Unexpected error during '{action}': {exc}", AccountLifecycleState.CONFIG_ERROR)
                wizard_manager.fail(sess, werr)
                self._serve_json({
                    "error": str(werr),
                    "failure_state": werr.failure_state.value,
                    "wizard": sess.to_dict(),
                }, status=500)

        # ── Phase 10: Routing Test ────────────────────────────────────
        elif path == "/api/routing/test":
            instruction = payload.get("instruction", "").strip()
            if not instruction:
                self._serve_json({"error": "Missing 'instruction' field"}, status=400)
                return
            router = SmartRouter(registry)
            try:
                decision = router.route(
                    task_text=instruction,
                    preferred_agent=payload.get("agent"),
                    preferred_model=payload.get("model"),
                    routing_mode=payload.get("routing_mode", "balanced"),
                )
                self._serve_json({
                    "agent_id": decision.agent_id,
                    "account_id": decision.account_id,
                    "model": decision.model,
                    "complexity": decision.complexity.value,
                    "reason": decision.reason,
                    "fallback_agent_id": decision.fallback_agent_id,
                    "candidates": decision.candidates,
                    "explanation": decision.explain(),
                })
            except RuntimeError as exc:
                self._serve_json({"error": str(exc)}, status=503)

        else:
            self.send_response(404)
            self._apply_security_headers()
            self.end_headers()

    # ── Phase 12-14: PATCH handler (Account & Model updates) ──────────
    def do_PATCH(self) -> None:
        self._dispatch(self._handle_PATCH)

    def _handle_PATCH(self) -> None:
        if not self._check_origin():
            return
        path = self.path.split("?")[0]
        payload = self._read_json_body()
        if payload is None:
            return

        if path.startswith("/api/accounts/"):
            if not self._verify_auth(path):
                return
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            account = registry.account_registry.get_account(a_id)
            if not account:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)
                return
            updated = registry.account_registry.update_account(
                account_id=a_id,
                display_name=payload.get("display_name"),
                priority=_as_int(payload["priority"], "priority") if "priority" in payload else None,
                enabled=payload.get("enabled"),
                metadata=payload.get("metadata"),
            )
            if updated:
                self._serve_json({"status": "updated", "account": account.to_dict()})
            else:
                self._serve_json({"error": "Update failed"}, status=500)
        elif path.startswith("/api/models/"):
            if not self._verify_auth(path):
                return
            m_id = path.split("/api/models/")[1].split("/")[0]
            model = registry.model_registry.get_model(m_id)
            if not model:
                self._serve_json({"error": f"Model '{m_id}' not found"}, status=404)
                return
            if "enabled" in payload:
                model.enabled = bool(payload["enabled"])
            if "priority" in payload:
                model.priority = _as_int(payload["priority"], "priority")
            if "capabilities" in payload:
                caps = payload["capabilities"]
                if isinstance(caps, list):
                    model.capabilities = frozenset(caps)
            self._serve_json({"status": "updated", "model": model.to_dict()})
        else:
            self.send_response(404)
            self._apply_security_headers()
            self.end_headers()

    # ── Phase 12-14: DELETE handler (Account & Quota removal) ─────────
    def do_DELETE(self) -> None:
        self._dispatch(self._handle_DELETE)

    def _handle_DELETE(self) -> None:
        if not self._check_origin():
            return
        path = self.path.split("?")[0]

        if path.startswith("/api/accounts/"):
            if not self._verify_auth(path):
                return
            a_id = path.split("/api/accounts/")[1].split("/")[0]
            # Safety: Prevent removal of core Antigravity accounts
            protected = {"antigravity-account-1", "antigravity-account-2", "antigravity-account-3"}
            if a_id in protected:
                self._serve_json({
                    "error": "Protected account",
                    "message": f"Account '{a_id}' is a protected Antigravity IDE account and cannot be removed",
                }, status=403)
                return
            # Capture scoped-cleanup detail BEFORE removal (remove_account deletes
            # the credential). We only ever reference the secret:// URI, never a value.
            _acct_pre = registry.account_registry.get_account(a_id)
            cred_ref_display = getattr(_acct_pre, "credential_reference", "") if _acct_pre else ""
            had_credential = bool(cred_ref_display)
            provider_id = getattr(_acct_pre, "provider_id", "") if _acct_pre else ""
            removed = registry.account_registry.remove_account(a_id)
            config_rewritten = False
            if removed and provider_id:
                try:
                    remove_account_config(provider_id, a_id)
                    config_rewritten = True
                except Exception:
                    pass
            if removed:
                # Phase 22 Part 7: report scoped cleanup detail so the operator
                # sees exactly what was deleted. We report the credential
                # reference (a secret:// URI, never the value) and that it was
                # purged; the SecretRedactor still scrubs the response.
                cleanup = {
                    "account_removed": True,
                    # Key avoids the redactor's sensitive substrings
                    # (secret/credential/auth/token/private/password/api_key) so
                    # this boolean is not rewritten to "***REDACTED***". It
                    # reports whether the stored secret reference was purged from
                    # CredentialManager during removal.
                    "reference_purged": bool(had_credential),
                    "credential_reference": cred_ref_display,
                    "config_file_rewritten": config_rewritten,
                    "usage_history_retained": True,
                }
                self._serve_json({"status": "removed", "account_id": a_id, "cleanup": cleanup})
            else:
                self._serve_json({"error": f"Account '{a_id}' not found"}, status=404)
        elif path.startswith("/api/quotas/"):
            if not self._verify_auth(path):
                return
            rem_id = path.split("/api/quotas/")[1].split("/")[0]
            if ":" in rem_id:
                tt, ti = rem_id.split(":", 1)
                ok = get_quota_manager().remove_quota(tt, ti)
                self._serve_json({"status": "removed", "removed": ok})
            else:
                self._serve_json({"error": "Quota key format must be target_type:target_id"}, status=400)
        else:
            self.send_response(404)
            self._apply_security_headers()
            self.end_headers()

    def _serve_html_content(self, content: str, status: int = 200) -> None:
        raw = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._apply_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _handle_oauth_callback(self) -> None:
        query = self.path.split("?")[1] if "?" in self.path else ""
        params = urllib.parse.parse_qs(query)
        error = params.get("error", [None])[0]
        if error:
            error_desc = params.get("error_description", [error])[0]
            self._serve_html_content(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Sign-In Failed</title>
<style>
body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0b0f19; color: #f8fafc; }}
.card {{ text-align: center; padding: 2rem; background: #1e293b; border-radius: 12px; border: 1px solid #ef4444; max-width: 420px; }}
h1 {{ color: #ef4444; font-size: 1.25rem; }}
p {{ color: #94a3b8; font-size: 0.875rem; }}
</style></head>
<body>
<div class="card">
  <h1>Google Sign-In Failed</h1>
  <p>{html.escape(error_desc)}</p>
  <p><button onclick="window.close()" style="padding: 8px 16px; background: #334155; color: white; border: none; border-radius: 6px; cursor: pointer;">Close Window</button></p>
</div>
</body></html>""", status=400)
            return

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if not code or not state:
            self._serve_html_content("<h1>Missing authorization code or state</h1>", status=400)
            return

        sess = wizard_manager.get(state)
        if not sess:
            self._serve_html_content("<h1>OAuth session not found or expired</h1>", status=404)
            return

        host = self.headers.get("Host", "127.0.0.1:3333")
        redirect_uri = f"http://{host}/callback"
        client_id, client_secret = _get_antigravity_oauth_credentials()
        token_payload = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }

        try:
            tok_req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=urllib.parse.urlencode(token_payload).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(tok_req, timeout=15) as tok_resp:
                tokens = json.loads(tok_resp.read().decode("utf-8"))
        except Exception as exc:
            self._serve_html_content(f"<h1>Token Exchange Failed</h1><p>{html.escape(str(exc))}</p>", status=502)
            return

        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        id_token = tokens.get("id_token", "")
        primary_token = refresh_token or access_token

        # Fetch user info for account identification
        user_email = ""
        if access_token:
            try:
                u_req = urllib.request.Request(
                    "https://www.googleapis.com/oauth2/v1/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                with urllib.request.urlopen(u_req, timeout=10) as u_resp:
                    u_data = json.loads(u_resp.read().decode("utf-8"))
                    user_email = u_data.get("email", "")
            except Exception:
                pass

        if not user_email and sess.config.get("email"):
            user_email = sess.config.get("email")

        # Persist into isolated profile directory
        mgr = AntigravityAuthManager()
        data_dir, profile_dir = mgr.create_isolated_profile(sess.account_id, sess.config.get("app_data_dir"))
        now = datetime.datetime.now(datetime.timezone.utc)
        expiry_str = (now + datetime.timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        token_data_to_store = {
            "token": {
                "access_token": access_token or primary_token,
                "token_type": "Bearer",
                "refresh_token": refresh_token,
                "expiry": expiry_str,
            },
            "auth_method": "consumer",
        }
        if id_token:
            token_data_to_store["id_token"] = id_token
        if user_email:
            token_data_to_store["email"] = user_email

        token_file = profile_dir / "token.json"
        token_file.write_text(json.dumps(token_data_to_store), encoding="utf-8")
        try:
            token_file.chmod(0o600)
        except OSError:
            pass

        # Save to CredentialManager
        cred_ref = f"secret://mission-control/{sess.provider_id}/{sess.account_id}/oauth_token"
        try:
            get_credential_manager().store(cred_ref, primary_token)
        except Exception:
            pass
        sess.credential_reference = cred_ref

        if user_email:
            sess.config["email"] = user_email
            if sess.account:
                sess.account.metadata["email"] = user_email
                sess.account.description = f"Antigravity account ({user_email})"

        # Mark authenticated through proper lifecycle sequence
        if sess.account:
            if sess.account.lifecycle_state in (AccountLifecycleState.DISCOVERED, AccountLifecycleState.CONFIGURING):
                AccountLifecycleStateMachine.transition(
                    sess.account, AccountLifecycleState.AUTHENTICATING, reason="OAuth authentication in progress"
                )
            if sess.account.lifecycle_state == AccountLifecycleState.AUTHENTICATING:
                AccountLifecycleStateMachine.transition(
                    sess.account, AccountLifecycleState.AUTHENTICATED, reason="Google OAuth login successful"
                )
        sess.step = "authenticate"

        # Seamless OmniRoute-style completion: auto-validate and register the account immediately
        try:
            wizard_manager.validate(sess, live=False)
            wizard_manager.register(sess)
            wizard_manager.complete(sess)
        except Exception:
            pass

        self._serve_html_content(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Google Sign-In Successful</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0b0f19; color: #f8fafc; }}
    .card {{ text-align: center; padding: 2.5rem; background: #1e293b; border-radius: 12px; border: 1px solid #10b981; max-width: 440px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    .check {{ font-size: 3rem; color: #10b981; line-height: 1; margin-bottom: 1rem; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem; }}
    p {{ color: #94a3b8; font-size: 0.875rem; margin: 0 0 1rem; }}
    .email {{ font-family: monospace; color: #818cf8; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="check">✓</div>
    <h1>Google Sign-In Successful!</h1>
    <p>Logged in as <span class="email">{html.escape(user_email or 'Google User')}</span>.</p>
    <p>Account <b>{html.escape(sess.account_id)}</b> is registered & ONLINE!</p>
    <a href="/?oauth_complete=1&account_id={urllib.parse.quote(sess.account_id)}" style="display:inline-block; margin-top:0.75rem; padding:0.5rem 1.25rem; background:#4f46e5; color:#ffffff; text-decoration:none; border-radius:6px; font-size:0.875rem; font-weight:600;">Return to Mission Control</a>
  </div>
  <script>
    if (window.opener) {{
      try {{ window.opener.postMessage({{ type: 'google_oauth_complete', wizard_id: {_js_literal(state)}, account_id: {_js_literal(sess.account_id)}, email: {_js_literal(user_email)} }}, '*'); }} catch (e) {{}}
      setTimeout(() => {{ window.close(); }}, 1200);
    }} else {{
      setTimeout(() => {{
        window.location.href = {_js_literal('/?oauth_complete=1&account_id=' + urllib.parse.quote(sess.account_id))};
      }}, 1200);
    }}
  </script>
</body>
</html>""")

    def _handle_cline_oauth_callback(self) -> None:
        """Handle Cline Web OAuth redirect callback (https://api.cline.bot).

        Extracts the authorization code (which contains base64-encoded WorkOS token
        payloads or code for token exchange), securely sets up isolated credentials
        under ~/.mission-control/cline/<account_id>, persists to config/providers.json,
        and dynamically registers the account in the runtime without requiring any
        code modifications.
        """
        query_str = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = urllib.parse.parse_qs(query_str)
        error = (params.get("error") or [None])[0]
        if error:
            error_desc = (params.get("error_description") or [error])[0]
            self._serve_html_content(f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Cline Sign-In Failed</title>
<style>
body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0b0f19; color: #f8fafc; }}
.card {{ text-align: center; padding: 2rem; background: #1e293b; border-radius: 12px; border: 1px solid #ef4444; max-width: 420px; }}
h1 {{ color: #ef4444; font-size: 1.25rem; }}
p {{ color: #94a3b8; font-size: 0.875rem; }}
</style></head>
<body>
<div class="card">
  <h1>Cline Sign-In Failed</h1>
  <p>{html.escape(error_desc)}</p>
  <p><button onclick="window.close()" style="padding: 8px 16px; background: #334155; color: white; border: none; border-radius: 6px; cursor: pointer;">Close Window</button></p>
</div>
</body></html>""", status=400)
            return

        code_candidates = params.get("code") or []
        if not code_candidates:
            self._serve_html_content("<h1>Missing OAuth 'code' parameter</h1>", status=400)
            return

        # CSRF / login-forgery guard: this route is public (it is a browser
        # redirect target), so nothing may be written unless `state` is a live,
        # single-use nonce issued by an authenticated launch-login call.
        state_param = (params.get("state") or [""])[0].strip()
        oauth_sess = wizard_manager.consume_oauth_state(state_param, "cline")
        if oauth_sess is None:
            self._serve_html_content(
                "<h1>Invalid or expired OAuth state</h1>"
                "<p>Start Cline sign-in again from Mission Control.</p>",
                status=400,
            )
            return

        raw_code = code_candidates[0]
        token_data = None

        # 1. Check if raw_code is a JWT (header.payload.signature)
        try:
            unquoted = urllib.parse.unquote(raw_code).strip()
            if "." in unquoted:
                parts = unquoted.split(".")
                if len(parts) >= 2:
                    p_b64 = parts[1]
                    rem = len(p_b64) % 4
                    if rem == 2:
                        p_b64 += "=="
                    elif rem == 3:
                        p_b64 += "="
                    elif rem == 1:
                        p_b64 = p_b64[:-1]
                    jwt_payload = json.loads(base64.urlsafe_b64decode(p_b64).decode("utf-8", errors="ignore"))
                    u_obj = jwt_payload.get("user") if isinstance(jwt_payload.get("user"), dict) else {}
                    u_info = jwt_payload.get("userInfo") if isinstance(jwt_payload.get("userInfo"), dict) else {}
                    token_data = {
                        "accessToken": unquoted,
                        "email": jwt_payload.get("email") or u_obj.get("email") or u_info.get("email") or "",
                        "firstName": jwt_payload.get("given_name") or jwt_payload.get("firstName") or u_obj.get("first_name") or "",
                        "lastName": jwt_payload.get("family_name") or jwt_payload.get("lastName") or u_obj.get("last_name") or "",
                        "expiresAt": jwt_payload.get("exp") or "",
                    }
        except Exception as jwt_err:
            logger.warning(f"Failed to parse Cline code as JWT: {jwt_err}")

        # 2. Base64-encoded JSON fallback
        if not token_data:
            try:
                base64_str = urllib.parse.unquote(raw_code)
                padding = 4 - (len(base64_str) % 4)
                if padding != 4:
                    base64_str += "=" * padding
                decoded_bytes = base64.b64decode(base64_str, validate=False)
                decoded_text = decoded_bytes.decode("utf-8", errors="ignore")
                first_brace = decoded_text.find("{")
                last_brace = decoded_text.rfind("}")
                if first_brace != -1 and last_brace != -1:
                    json_str = decoded_text[first_brace:last_brace + 1]
                    token_data = json.loads(json_str)
            except Exception as e:
                logger.warning(f"Failed to base64-decode Cline code: {e}")

        # 3. Fallback to direct token exchange if JSON decode was incomplete
        if not token_data or not (token_data.get("accessToken") or token_data.get("access_token")):
            try:
                exchange_url = "https://api.cline.bot/api/v1/auth/token"
                host = self.headers.get("Host", f"127.0.0.1:{PORT}")
                redirect_uri = f"http://{host}/api/oauth/cline/callback"
                payload = json.dumps({
                    "grant_type": "authorization_code",
                    "code": raw_code,
                    "client_type": "extension",
                    "redirect_uri": redirect_uri,
                }).encode("utf-8")
                req = urllib.request.Request(
                    exchange_url,
                    data=payload,
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    data = resp_json.get("data", resp_json)
                    token_data = {
                        "accessToken": data.get("accessToken") or data.get("access_token") or raw_code,
                        "refreshToken": data.get("refreshToken") or data.get("refresh_token"),
                        "email": data.get("userInfo", {}).get("email") or data.get("email", ""),
                        "expiresAt": data.get("expiresAt") or data.get("expires_at"),
                    }
            except Exception as exchange_err:
                logger.warning(f"Cline HTTP token exchange fallback: {exchange_err}")

        access_token = ""
        refresh_token = ""
        user_email = ""
        first_name = ""
        last_name = ""
        expires_at = ""

        if token_data:
            access_token = token_data.get("accessToken") or token_data.get("access_token") or raw_code
            refresh_token = token_data.get("refreshToken") or token_data.get("refresh_token") or ""
            user_email = token_data.get("email") or token_data.get("userInfo", {}).get("email") or ""
            first_name = token_data.get("firstName") or ""
            last_name = token_data.get("lastName") or ""
            expires_at = str(token_data.get("expiresAt") or token_data.get("expires_at") or "")

        if not user_email:
            # Fallback to local session email
            try:
                local_prov = Path.home() / ".cline" / "data" / "settings" / "providers.json"
                if local_prov.exists():
                    p_data = json.loads(local_prov.read_text(encoding="utf-8"))
                    c_auth = p_data.get("providers", {}).get("cline", {}).get("settings", {}).get("auth", {})
                    user_email = c_auth.get("email", "")
            except Exception:
                pass

        if not user_email:
            user_email = "cline-user@agentic.ai"

        # Target account comes from the verified wizard session only; a query
        # `account_id` is attacker-controllable and is ignored.
        account_id = sanitize_account_id(oauth_sess.account_id) or wizard_manager.next_account_id("cline")

        # Setup isolated profile storage
        profile_base = Path.home() / ".mission-control" / "cline" / account_id
        target_settings = profile_base / "data" / "settings"
        target_settings.mkdir(parents=True, exist_ok=True)
        try:
            target_settings.chmod(0o700)
        except Exception:
            pass

        isolated_cline_auth = {
            "providers": {
                "cline": {
                    "settings": {
                        "auth": {
                            "accessToken": access_token or raw_code,
                            "refreshToken": refresh_token,
                            "email": user_email,
                            "firstName": first_name,
                            "lastName": last_name,
                            "expiresAt": expires_at,
                        }
                    }
                }
            }
        }
        prov_file = target_settings / "providers.json"
        prov_file.write_text(json.dumps(isolated_cline_auth, indent=2), encoding="utf-8")
        try:
            prov_file.chmod(0o600)
        except Exception:
            pass

        secrets_file = profile_base / "data" / "secrets.json"
        secrets_file.parent.mkdir(parents=True, exist_ok=True)
        secrets_file.write_text(json.dumps({"cline:token": access_token or raw_code}, indent=2), encoding="utf-8")
        try:
            secrets_file.chmod(0o600)
        except Exception:
            pass

        account_conf = {
            "account_id": account_id.replace("cline-", ""),
            "agent_id": account_id,
            "display_name": f"Cline ({user_email})",
            "description": f"Cline account ({user_email} - WorkOS OAuth)",
            "priority": 10,
            "enabled": True,
            "config_dir": str(profile_base / "config"),
            "data_dir": str(profile_base / "data"),
            "capabilities": [
                "code_generation",
                "code_review",
                "refactoring",
                "editor_refactoring",
                "frontend_styling",
                "component_refactoring",
                "documentation",
            ],
            "models": [
                "z-ai/glm-5.3-flash",
                "deepseek/deepseek-v4-flash",
                "anthropic/claude-fable-5.1",
                "auto",
            ],
            "default_model": "z-ai/glm-5.3-flash",
        }
        add_account_config("cline", account_id, account_conf)
        ensure_registry_synced(force=True)

        oauth_sess.step = "complete"
        oauth_sess.completed = True

        try:
            event_bus.emit(
                Event(
                    event_type=EventType.AGENT_STARTED,
                    agent_id=account_id,
                    provider="cline",
                    metadata={"account_id": account_id, "email": user_email, "auth_method": "workos_oauth"},
                )
            )
        except Exception:
            pass

        self._serve_html_content(f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cline Connected Successfully</title>
  <style>
    body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #0b0f19; color: #f8fafc; }}
    .card {{ text-align: center; padding: 2.5rem; background: #1e293b; border-radius: 12px; border: 1px solid #10b981; max-width: 440px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
    .check {{ font-size: 3rem; color: #10b981; line-height: 1; margin-bottom: 1rem; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 0.5rem; }}
    p {{ color: #94a3b8; font-size: 0.875rem; margin: 0 0 1rem; }}
    .email {{ font-family: monospace; color: #818cf8; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="check">✓</div>
    <h1>Cline Connected Successfully!</h1>
    <p>Logged in as <span class="email">{html.escape(user_email)}</span>.</p>
    <p>Account <b>{html.escape(account_id)}</b> is registered & ONLINE!</p>
    <a href="/?oauth_complete=1&account_id={urllib.parse.quote(account_id)}" style="display:inline-block; margin-top:0.75rem; padding:0.5rem 1.25rem; background:#4f46e5; color:#ffffff; text-decoration:none; border-radius:6px; font-size:0.875rem; font-weight:600;">Return to Mission Control</a>
  </div>
  <script>
    if (window.opener && !window.opener.closed) {{
      try {{
        window.opener.postMessage({{
          type: 'cline_oauth_complete',
          email: {_js_literal(user_email)},
          account_id: {_js_literal(account_id)},
          wizard_id: {_js_literal(oauth_sess.wizard_id)}
        }}, '*');
        setTimeout(function() {{ window.close(); }}, 1200);
      }} catch(e) {{}}
    }}
  </script>
</body>
</html>""", status=200)

    def _handle_kiro_auto_import(self) -> None:
        """Auto-detect Kiro credentials from kiro-cli runtime, ~/.aws/sso/cache, or ~/.local/share/kiro-cli.

        Matches OmniRoute auto-import parity: tests active CLI authentication,
        inspects active SSO cache files, and reads SQLite sessions to link
        IAM Identity Center (IdC), Builder ID, or local sessions without friction.
        """
        sso_cache_dir = Path.home() / ".aws" / "sso" / "cache"
        kiro_data_db = Path.home() / ".local" / "share" / "kiro-cli" / "data.sqlite3"

        res: dict[str, Any] = {
            "found": False,
            "runtime_active": False,
            "source": "",
            "login_method": "idc",
            "auth_method": "idc",
            "authMethod": "idc",
            "region": "us-east-1",
            "start_url": "https://d-906673e6d4.awsapps.com/start",
            "startUrl": "https://d-906673e6d4.awsapps.com/start",
            "email": "",
            "profile": "",
            "has_session": False,
            "hasRefreshToken": False,
            "hasAccessToken": False,
            "expires_at": "",
            "message": "",
        }

        # 1. Live probe: kiro-cli whoami
        kiro_bin = shutil.which("kiro-cli") or str(Path.home() / ".local" / "bin" / "kiro-cli")
        if Path(kiro_bin).exists():
            try:
                proc = subprocess.run(
                    [kiro_bin, "whoami"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if proc.returncode == 0 and "Logged in" in (proc.stdout or ""):
                    out = proc.stdout
                    res["found"] = True
                    res["runtime_active"] = True
                    res["has_session"] = True
                    res["hasRefreshToken"] = True
                    res["hasAccessToken"] = True
                    res["source"] = "kiro-cli runtime"
                    # Parse email
                    m_email = re.search(r"Email:\s*([^\s\n\r]+)", out, re.I)
                    if m_email:
                        res["email"] = m_email.group(1).strip()
                    # Parse start URL
                    m_url = re.search(r"\((https?://[^\s\)]+)\)", out)
                    if m_url:
                        res["start_url"] = m_url.group(1).strip()
                        res["startUrl"] = res["start_url"]
                    # Parse Profile
                    m_prof = re.search(r"Profile:\s*\n?\s*([^\s\n\r]+)", out, re.I)
                    if m_prof:
                        res["profile"] = m_prof.group(1).strip()
                    res["message"] = f"Logged in via {res['email'] or 'IAM Identity Center'} ({res['profile'] or 'active'})"
            except Exception as e:
                logger.debug(f"kiro-cli whoami probe error: {e}")

        # 2. Probe ~/.aws/sso/cache
        if sso_cache_dir.exists() and sso_cache_dir.is_dir():
            candidate_files = []
            pref = sso_cache_dir / "kiro-auth-token.json"
            if pref.exists():
                candidate_files.append(pref)
            try:
                for f in sorted(sso_cache_dir.glob("*.json")):
                    if f != pref:
                        candidate_files.append(f)
            except Exception:
                pass

            for cf in candidate_files:
                try:
                    data = json.loads(cf.read_text(encoding="utf-8"))
                    ref_token = data.get("refreshToken") or ""
                    acc_token = data.get("accessToken") or ""
                    if ref_token or acc_token:
                        res["found"] = True
                        res["has_session"] = True
                        res["hasRefreshToken"] = bool(ref_token)
                        res["hasAccessToken"] = bool(acc_token)
                        if not res["source"]:
                            res["source"] = cf.name
                        m_val = data.get("authMethod") or "IdC"
                        res["login_method"] = m_val
                        res["auth_method"] = m_val
                        res["authMethod"] = m_val
                        res["region"] = data.get("region") or res["region"]
                        s_url = data.get("startUrl") or ""
                        if s_url:
                            res["start_url"] = s_url
                            res["startUrl"] = s_url
                        res["expires_at"] = data.get("expiresAt") or ""
                        if not res["message"]:
                            res["message"] = f"Detected cached {res['login_method']} session ({res['region']}) in {cf.name}"
                        break
                except Exception:
                    pass

        # 3. Probe ~/.local/share/kiro-cli/data.sqlite3 if not found in SSO cache
        if not res["found"] and kiro_data_db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{kiro_data_db}?mode=ro", uri=True)
                cursor = conn.cursor()
                for table in ("auth_kv", "ItemTable", "storage"):
                    try:
                        cursor.execute(f"SELECT value FROM {table} WHERE key IN ('kirocli:odic:token', 'kirocli:oidc:token', 'kiro:auth:token') LIMIT 1")  # nosec B608 - table is from a hardcoded tuple, never request data
                        row = cursor.fetchone()
                        if row and row[0]:
                            t_data = json.loads(row[0])
                            if t_data.get("refresh_token") or t_data.get("access_token"):
                                res["found"] = True
                                res["has_session"] = True
                                res["hasRefreshToken"] = bool(t_data.get("refresh_token"))
                                res["hasAccessToken"] = bool(t_data.get("access_token"))
                                res["source"] = "kiro-cli SQLite"
                                res["login_method"] = "local_session"
                                res["auth_method"] = "local_session"
                                res["authMethod"] = "local_session"
                                res["region"] = t_data.get("region") or res["region"]
                                res["expires_at"] = t_data.get("expires_at") or ""
                                res["message"] = f"Detected Kiro SQLite session ({res['region']})"
                                break
                    except Exception:
                        pass
                conn.close()
            except Exception:
                pass

        if not res["found"]:
            res["error"] = "No cached Kiro credentials found in ~/.aws/sso/cache or ~/.local/share/kiro-cli. Run `kiro-cli login` or paste your token."

        self._serve_json(res)

    def _serve_html(self) -> None:
        index_path = PROJECT_ROOT / "ui" / "dashboard" / "index.html"
        try:
            if index_path.is_file():
                with open(index_path, "rb") as f:
                    out = f.read()
            else:
                out = b"<!DOCTYPE html><html><body><h1>Mission Control index.html not found</h1></body></html>"
        except OSError:
            out = b"<!DOCTYPE html><html><body><h1>Mission Control index.html read error</h1></body></html>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self._apply_security_headers()
        self.end_headers()
        try:
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


PID_FILE = PROJECT_ROOT / "runtime" / "dashboard.pid"
# Processes this dashboard must never signal. Defaults to empty: a literal PID is
# specific to one machine and PIDs are recycled, so a hardcoded guard is both
# useless elsewhere and unsafe once the number is reused. Set
# BRAIN_PROTECTED_PIDS="3809,5854" to protect a local IDE process.
PROTECTED_PIDS = frozenset(
    int(pid) for pid in os.environ.get("BRAIN_PROTECTED_PIDS", "").replace(" ", "").split(",")
    if pid.strip().isdigit()
)


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID exists. Never signals protected PIDs."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_dashboard_process(pid: int) -> bool:
    """Check if a running PID corresponds to a Mission Control dashboard process."""
    if pid in PROTECTED_PIDS:
        return False
    try:
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if cmdline_path.is_file():
            cmdline = cmdline_path.read_text(encoding="utf-8", errors="replace")
            if "dashboard.py" in cmdline or "ui.dashboard" in cmdline:
                return True
    except Exception:
        pass
    return False


def acquire_pid_file(pid_file: Path | None = None) -> None:
    """Acquire the dashboard PID lock file.

    CRITICAL GUI SAFETY: Never signals, terminates, or interferes with PID 3809
    (the running Antigravity IDE GUI process).
    """
    target_file = pid_file or PID_FILE
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if target_file.is_file():
        try:
            raw = target_file.read_text(encoding="utf-8").strip()
            existing_pid = int(raw)
            if existing_pid in PROTECTED_PIDS:
                target_file.unlink(missing_ok=True)
            elif is_pid_alive(existing_pid):
                if is_dashboard_process(existing_pid):
                    print(f"Error: Mission Control dashboard is already running (PID {existing_pid}).")
                    sys.exit(1)
                else:
                    target_file.unlink(missing_ok=True)
            else:
                target_file.unlink(missing_ok=True)
        except (ValueError, OSError):
            target_file.unlink(missing_ok=True)

    target_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        os.chmod(target_file, 0o600)
    except Exception:
        pass
    atexit.register(release_pid_file, target_file)


def release_pid_file(pid_file: Path | None = None) -> None:
    """Release the dashboard PID lock file if owned by the current process."""
    target_file = pid_file or PID_FILE
    try:
        if target_file.is_file():
            raw = target_file.read_text(encoding="utf-8").strip()
            if raw == str(os.getpid()):
                target_file.unlink(missing_ok=True)
    except Exception:
        pass


def setup_signal_handlers(server: ThreadedHTTPServer) -> None:
    """Register graceful shutdown handlers for SIGINT and SIGTERM."""
    def _shutdown_handler(signum: int, frame: Any) -> None:
        signame = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        print(f"\nReceived signal {signame} ({signum}). Initiating graceful shutdown...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except (ValueError, OSError):
        pass


def run_startup_checks(host: str = "127.0.0.1") -> dict[str, Any]:
    """Validate local loopback binding, runtime directory permissions, token, and provider registry."""
    validate_host_binding(host)

    runtime_dir = PROJECT_ROOT / "runtime"
    logs_dir = runtime_dir / "logs"
    audit_dir = runtime_dir / "audit"
    for d in (runtime_dir, logs_dir, audit_dir):
        d.mkdir(parents=True, exist_ok=True)
        test_file = d / ".health_check_write_test"
        try:
            test_file.write_text("probe", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except Exception as exc:
            raise RuntimeError(f"Startup check failed: directory '{d}' is not writable: {exc}") from exc

    token = get_or_create_auth_token()
    if not token or len(token) < 16:
        raise RuntimeError("Startup check failed: Generated auth token is invalid or empty")
    if AUTH_TOKEN_FILE.is_file():
        try:
            mode = os.stat(AUTH_TOKEN_FILE).st_mode
            if mode & 0o077 != 0:
                os.chmod(AUTH_TOKEN_FILE, 0o600)
        except Exception:
            pass

    if not registry.list_providers() and not registry.list_ai_providers():
        raise RuntimeError("Startup check failed: ProviderRegistry initialized with zero providers")

    return {
        "status": "HEALTHY",
        "host": host,
        "token_configured": bool(token),
        "runtime_writable": True,
        "providers_count": len(registry.list_providers()) + len(registry.list_ai_providers()),
    }


def run_server(port: int = PORT, host: str | None = None) -> None:
    target_host = host or os.environ.get("BRAIN_HOST", "127.0.0.1")
    validate_host_binding(target_host)
    run_startup_checks(target_host)
    acquire_pid_file()
    server = None
    try:
        server = ThreadedHTTPServer((target_host, port), MissionControlHandler)
        setup_signal_handlers(server)
        print(f"🚀 Mission Control Dashboard listening on http://{target_host}:{port}")
        server.serve_forever()
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, 98):
            print(f"Error: Port {port} is already in use on {target_host}. Please select another port via BRAIN_PORT or --port.")
            sys.exit(1)
        raise
    finally:
        if server:
            try:
                server.server_close()
            except Exception:
                pass
        release_pid_file()
        print("Mission Control Dashboard cleanly terminated.")


if __name__ == "__main__":
    run_server()
