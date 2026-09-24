"""First-Class Account Abstraction and Account Pool Management.

Manages multiple isolated accounts per provider, healthy account rotation,
rate limit cooldowns, concurrency bounds, and failover pools without
exposing secrets.
"""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from providers.registry.credential_manager import get_credential_manager
from providers.registry.lifecycle import (
    AccountLifecycleState,
    AccountLifecycleStateMachine,
    AuthState,
    HealthState,
    InvalidLifecycleTransitionError,
    ProcessState,
    TaskExecutionState,
)

logger = logging.getLogger(__name__)


class AuthenticationType(str, Enum):
    """Authentication mechanisms for accounts."""
    API_KEY = "api_key"
    OAUTH = "oauth"
    SERVICE_ACCOUNT = "service_account"
    ENVIRONMENT = "environment"
    LOCAL = "local"
    CUSTOM = "custom"
    UNAUTHENTICATED = "unauthenticated"
    ENV_VAR = "environment"


class AccountStatus(str, Enum):
    """Standard health/lifecycle status of an account."""
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"
    COOLDOWN = "COOLDOWN"
    CONFIG_ERROR = "CONFIG_ERROR"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    UNKNOWN = "UNKNOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


@dataclass
class Account:
    """First-class Account representation in Mission Control."""
    id: str
    provider_id: str
    account_name: str
    display_name: str = ""
    account_type: str = "api"  # api, agent, ide, gateway
    authentication_type: AuthenticationType = AuthenticationType.API_KEY
    credential_reference: str = ""
    endpoint: str | None = None
    status: AccountStatus = AccountStatus.UNKNOWN
    enabled: bool = True
    priority: int = 10
    models: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_health_check: str | None = None
    concurrency_limit: int = 2
    current_concurrency: int = 0
    failure_count: int = 0
    cooldown_until: float | None = None
    # Decoupled Phase 22 states
    lifecycle_state: AccountLifecycleState = AccountLifecycleState.DISCOVERED
    auth_state: AuthState = AuthState.UNAUTHENTICATED
    health_state: HealthState = HealthState.UNKNOWN
    process_state: ProcessState = ProcessState.IDLE
    task_state: str = "IDLE"
    health_reason: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.account_name

        if isinstance(self.authentication_type, str):
            try:
                self.authentication_type = AuthenticationType(self.authentication_type)
            except ValueError:
                pass
        if isinstance(self.status, str):
            try:
                self.status = AccountStatus(self.status)
            except ValueError:
                pass
        if isinstance(self.lifecycle_state, str):
            try:
                self.lifecycle_state = AccountLifecycleState(self.lifecycle_state)
            except ValueError:
                pass
        if isinstance(self.auth_state, str):
            try:
                self.auth_state = AuthState(self.auth_state)
            except ValueError:
                pass
        if isinstance(self.health_state, str):
            try:
                self.health_state = HealthState(self.health_state)
            except ValueError:
                pass
        if isinstance(self.process_state, str):
            try:
                self.process_state = ProcessState(self.process_state)
            except ValueError:
                pass

        # Align states if lifecycle_state was default DISCOVERED but status was provided
        if self.lifecycle_state == AccountLifecycleState.DISCOVERED and self.status != AccountStatus.UNKNOWN:
            if self.status == AccountStatus.ONLINE:
                self.lifecycle_state = AccountLifecycleState.ONLINE
                self.auth_state = AuthState.AUTHENTICATED
                self.health_state = HealthState.HEALTHY
            elif self.status in (AccountStatus.DEGRADED, AccountStatus.RATE_LIMITED, AccountStatus.COOLDOWN, AccountStatus.QUOTA_EXHAUSTED):
                self.lifecycle_state = AccountLifecycleState.ONLINE
                self.auth_state = AuthState.AUTHENTICATED
                self.health_state = HealthState.DEGRADED
            elif self.status == AccountStatus.AUTH_ERROR:
                self.lifecycle_state = AccountLifecycleState.AUTH_FAILED
                self.auth_state = AuthState.AUTH_FAILED
                self.health_state = HealthState.UNHEALTHY
            elif self.status == AccountStatus.CONFIG_ERROR:
                self.lifecycle_state = AccountLifecycleState.CONFIG_ERROR
                self.health_state = HealthState.UNHEALTHY
            elif self.status == AccountStatus.OFFLINE:
                self.lifecycle_state = AccountLifecycleState.OFFLINE
                self.health_state = HealthState.UNHEALTHY
            elif self.status == AccountStatus.DISABLED:
                self.lifecycle_state = AccountLifecycleState.DISABLED
                self.enabled = False
        elif self.lifecycle_state != AccountLifecycleState.DISCOVERED and self.status == AccountStatus.UNKNOWN:
            AccountLifecycleStateMachine.sync_status_field(self)

    @property
    def allowed_models(self) -> list[str]:
        return self.models

    def is_available(self, model: str | None = None) -> bool:
        """Check if account is enabled, healthy, within concurrency limits and not in cooldown."""
        if not self.enabled or self.status == AccountStatus.DISABLED:
            return False

        if self.lifecycle_state in (
            AccountLifecycleState.DISABLED,
            AccountLifecycleState.AUTH_FAILED,
            AccountLifecycleState.AUTH_CANCELLED,
            AccountLifecycleState.CONFIG_ERROR,
            AccountLifecycleState.PROVIDER_UNAVAILABLE,
            AccountLifecycleState.VALIDATION_FAILED,
        ):
            return False

        # Check if cooldown has expired
        if self.cooldown_until is not None:
            if time.time() >= self.cooldown_until:
                self.cooldown_until = None
                if self.status in (AccountStatus.RATE_LIMITED, AccountStatus.COOLDOWN):
                    self.status = AccountStatus.ONLINE
                    self.health_state = HealthState.HEALTHY
            else:
                return False

        if self.status in (AccountStatus.AUTH_ERROR, AccountStatus.CONFIG_ERROR, AccountStatus.OFFLINE, AccountStatus.RATE_LIMITED, AccountStatus.COOLDOWN, AccountStatus.QUOTA_EXHAUSTED):
            return False

        if self.health_state == HealthState.UNHEALTHY:
            return False

        if self.current_concurrency >= self.concurrency_limit:
            return False

        if model and self.models and model not in self.models and "auto" not in self.models:
            return False

        return True

    def to_dict(self, include_metadata: bool = True) -> dict[str, Any]:
        """Safe serialization: NEVER includes secrets or raw credentials."""
        res: dict[str, Any] = {
            "id": self.id,
            "provider_id": self.provider_id,
            "account_name": self.account_name,
            "display_name": self.display_name,
            "account_type": self.account_type,
            "authentication_type": self.authentication_type.value if isinstance(self.authentication_type, Enum) else str(self.authentication_type),
            "credential_reference": self.credential_reference,
            "endpoint": self.endpoint,
            "status": self.status.value if isinstance(self.status, Enum) else str(self.status),
            "enabled": self.enabled,
            "priority": self.priority,
            "models": self.models,
            "capabilities": self.capabilities,
            "concurrency_limit": self.concurrency_limit,
            "current_concurrency": self.current_concurrency,
            "failure_count": self.failure_count,
            "cooldown_active": bool(self.cooldown_until and time.time() < self.cooldown_until),
            "cooldown_until": self.cooldown_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_health_check": self.last_health_check,
            "usage": self.usage,
            "masked_key": get_credential_manager().get_masked_credential(self.credential_reference) if self.credential_reference else "",
            # Decoupled Phase 22 states
            "lifecycle_state": self.lifecycle_state.value if isinstance(self.lifecycle_state, Enum) else str(self.lifecycle_state),
            "auth_state": self.auth_state.value if isinstance(self.auth_state, Enum) else str(self.auth_state),
            "health_state": self.health_state.value if isinstance(self.health_state, Enum) else str(self.health_state),
            "process_state": self.process_state.value if isinstance(self.process_state, Enum) else str(self.process_state),
            "task_state": self.task_state.value if isinstance(self.task_state, Enum) else str(self.task_state),
            "health_reason": self.health_reason,
        }
        if include_metadata and self.metadata:
            # Metadata is sanitized by SecretRedactor
            res["metadata"] = get_credential_manager().redactor.redact_dict(self.metadata)
        return res


class AccountPool:
    """Pool of accounts for a single provider or routing group."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self._accounts: dict[str, Account] = {}

    def add_account(self, account: Account) -> None:
        self._accounts[account.id] = account

    def remove_account(self, account_id: str) -> Account | None:
        return self._accounts.pop(account_id, None)

    def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def list_accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def rotate(self) -> list[str]:
        """Safely rotate active account priorities within the pool."""
        accts = [a for a in self._accounts.values() if a.enabled]
        if len(accts) <= 1:
            return [a.id for a in accts]
        priorities = [a.priority for a in accts]
        rotated_priorities = priorities[1:] + priorities[:1]
        for a, p in zip(accts, rotated_priorities):
            a.priority = p
            a.updated_at = datetime.now(timezone.utc).isoformat()
        return [a.id for a in accts]

    def get_available_account(
        self,
        preferred_account: str | None = None,
        exclude_accounts: set[str] | None = None,
        model: str | None = None,
    ) -> Account | None:
        """Select an available account, preferring explicit selection if eligible."""
        exclude = exclude_accounts or set()

        if preferred_account and preferred_account in self._accounts:
            acc = self._accounts[preferred_account]
            if acc.id not in exclude and acc.is_available(model=model):
                return acc

        # Candidates: enabled, not in exclude, and is_available
        candidates = [
            a for a in self._accounts.values()
            if a.id not in exclude and a.is_available(model=model)
        ]
        if not candidates:
            return None

        # Sort by highest priority first, then lowest current concurrency, then lowest failure count
        candidates.sort(key=lambda a: (-a.priority, a.current_concurrency, a.failure_count))
        return candidates[0]

    def acquire(self, account_id: str) -> bool:
        acc = self._accounts.get(account_id)
        if not acc or not acc.is_available():
            return False
        acc.current_concurrency += 1
        return True

    def checkout(self, account_id: str) -> bool:
        """Alias for acquire."""
        return self.acquire(account_id)


    def release(self, account_id: str) -> None:
        acc = self._accounts.get(account_id)
        if acc and acc.current_concurrency > 0:
            acc.current_concurrency -= 1

    def record_success(self, account_id: str) -> None:
        acc = self._accounts.get(account_id)
        if acc:
            acc.failure_count = 0
            acc.cooldown_until = None
            acc.status = AccountStatus.ONLINE
            acc.lifecycle_state = AccountLifecycleState.ONLINE
            acc.auth_state = AuthState.AUTHENTICATED
            acc.health_state = HealthState.HEALTHY
            acc.health_reason = ""
            acc.last_health_check = datetime.now(timezone.utc).isoformat()

    def record_failure(
        self,
        account_id: str,
        is_rate_limit: bool = False,
        cooldown_seconds: float = 60.0,
        error: str | None = None,
    ) -> None:
        acc = self._accounts.get(account_id)
        if not acc:
            return
        acc.failure_count += 1
        acc.last_health_check = datetime.now(timezone.utc).isoformat()
        if error:
            acc.health_reason = str(error)
        if error and ("rate" in error.lower() or "429" in error or "quota" in error.lower()):
            is_rate_limit = True

        if is_rate_limit:
            acc.status = AccountStatus.RATE_LIMITED
            acc.health_state = HealthState.DEGRADED
            acc.cooldown_until = time.time() + cooldown_seconds
        else:
            # Apply progressive backoff for repeated non-rate-limit failures
            backoff = min(cooldown_seconds * (2 ** (acc.failure_count - 1)), 600.0)
            acc.cooldown_until = time.time() + backoff
            if acc.failure_count >= 3:
                acc.status = AccountStatus.OFFLINE
                acc.health_state = HealthState.UNHEALTHY
                acc.lifecycle_state = AccountLifecycleState.OFFLINE
            else:
                acc.health_state = HealthState.DEGRADED

    def transition_account_lifecycle(
        self,
        account_id: str,
        to_state: AccountLifecycleState | str,
        reason: str | None = None,
    ) -> AccountLifecycleState:
        acc = self.get_account(account_id)
        if not acc:
            raise KeyError(f"Account '{account_id}' not found in pool '{self.provider_id}'")
        return AccountLifecycleStateMachine.transition(acc, to_state, reason=reason)


class AccountRegistry:
    """Universal Account Registry tracking all accounts and pools across providers."""

    def __init__(self, auditor: Any | None = None) -> None:
        self._pools: dict[str, AccountPool] = {}
        self._accounts: dict[str, Account] = {}
        self._auditor = auditor

    def _audit(self) -> Any | None:
        """Lazily resolve the account auditor, tolerating import/runtime errors.

        Auditing must never break account management, so any failure to obtain
        or use the auditor is swallowed silently.
        """
        if self._auditor is not None:
            return self._auditor
        try:
            from brain.governance.audit_logger import get_account_auditor
            self._auditor = get_account_auditor()
        except Exception:
            self._auditor = None
        return self._auditor

    def get_pool(self, provider_id: str) -> AccountPool:
        if provider_id not in self._pools:
            self._pools[provider_id] = AccountPool(provider_id)
        return self._pools[provider_id]

    def register_account(self, account: Account) -> None:
        self._accounts[account.id] = account
        pool = self.get_pool(account.provider_id)
        pool.add_account(account)
        auditor = self._audit()
        if auditor is not None:
            try:
                from brain.governance.audit_logger import AccountAuditAction
                auditor.record(
                    action=AccountAuditAction.CREATE,
                    provider_id=account.provider_id,
                    account_id=account.id,
                    to_state=account.lifecycle_state,
                    details={"account_type": account.account_type},
                )
            except Exception:
                pass

    def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def list_accounts(self, provider_id: str | None = None) -> list[Account]:
        if provider_id:
            return self.get_pool(provider_id).list_accounts()
        return list(self._accounts.values())

    def get_accounts_for_provider(self, provider_id: str) -> list[Account]:
        """Return all accounts associated with a provider."""
        return self.list_accounts(provider_id=provider_id)

    def transition_account_lifecycle(
        self,
        account_id: str,
        to_state: AccountLifecycleState | str,
        reason: str | None = None,
        correlation_id: str | None = None,
    ) -> AccountLifecycleState:
        acc = self.get_account(account_id)
        if not acc:
            raise KeyError(f"Account '{account_id}' not found in registry")
        from_state = acc.lifecycle_state
        result = AccountLifecycleStateMachine.transition(acc, to_state, reason=reason)
        self._audit_transition(acc, from_state, result, reason, correlation_id)
        return result

    def _audit_transition(
        self,
        account: Account,
        from_state: AccountLifecycleState,
        to_state: AccountLifecycleState,
        reason: str | None,
        correlation_id: str | None = None,
    ) -> None:
        """Emit an ACCOUNT_LIFECYCLE_TRANSITION audit entry (best-effort)."""
        auditor = self._audit()
        if auditor is None:
            return
        try:
            from brain.governance.audit_logger import AccountAuditAction
            details = {"reason": reason} if reason else None
            auditor.record(
                action=AccountAuditAction.LIFECYCLE_TRANSITION,
                provider_id=account.provider_id,
                account_id=account.id,
                from_state=from_state,
                to_state=to_state,
                correlation_id=correlation_id,
                details=details,
            )
        except Exception:
            pass

    def enable_account(self, account_id: str) -> bool:
        acc = self._accounts.get(account_id)
        if acc:
            from_state = acc.lifecycle_state
            acc.enabled = True
            if acc.lifecycle_state == AccountLifecycleState.DISABLED or acc.status == AccountStatus.DISABLED:
                acc.lifecycle_state = AccountLifecycleState.ONLINE
                acc.status = AccountStatus.ONLINE
                acc.health_state = HealthState.HEALTHY
            auditor = self._audit()
            if auditor is not None:
                try:
                    from brain.governance.audit_logger import AccountAuditAction
                    auditor.record(
                        action=AccountAuditAction.ENABLE,
                        provider_id=acc.provider_id,
                        account_id=acc.id,
                        from_state=from_state,
                        to_state=acc.lifecycle_state,
                    )
                except Exception:
                    pass
            return True
        return False

    def disable_account(self, account_id: str) -> bool:
        acc = self._accounts.get(account_id)
        if acc:
            from_state = acc.lifecycle_state
            acc.enabled = False
            acc.lifecycle_state = AccountLifecycleState.DISABLED
            acc.status = AccountStatus.DISABLED
            auditor = self._audit()
            if auditor is not None:
                try:
                    from brain.governance.audit_logger import AccountAuditAction
                    auditor.record(
                        action=AccountAuditAction.DISABLE,
                        provider_id=acc.provider_id,
                        account_id=acc.id,
                        from_state=from_state,
                        to_state=acc.lifecycle_state,
                    )
                except Exception:
                    pass
            return True
        return False

    def set_account_enabled(self, account_id: str, enabled: bool) -> bool:
        return self.enable_account(account_id) if enabled else self.disable_account(account_id)


    def remove_account(self, account_id: str) -> bool:
        # Phase 8 Antigravity accounts are FROZEN and cannot be removed
        if account_id in {"antigravity-account-1", "antigravity-account-2", "antigravity-account-3"}:
            return False
        acc = self._accounts.pop(account_id, None)
        if not acc:
            return False
        pool = self._pools.get(acc.provider_id)
        if pool:
            pool.remove_account(account_id)
        # Delete secret reference if stored
        if acc.credential_reference:
            get_credential_manager().delete(acc.credential_reference)
        auditor = self._audit()
        if auditor is not None:
            try:
                from brain.governance.audit_logger import AccountAuditAction
                auditor.record(
                    action=AccountAuditAction.REMOVE,
                    provider_id=acc.provider_id,
                    account_id=acc.id,
                    from_state=acc.lifecycle_state,
                )
            except Exception:
                pass
        return True

    def safe_remove_account(
        self,
        account_id: str,
        correlation_id: str | None = None,
        remove_directories: bool = True,
        drain_timeout: float = 0.0,
    ) -> Any:
        """Idempotently and safely remove exactly one account (Phase 22, Part 13).

        Order of operations:
        1. Transition the account to ``DISABLED`` so the router immediately
           stops admitting it as a candidate.
        2. Drain (or refuse) in-flight work rather than killing it: the account
           is not deleted while it still holds concurrency, unless the caller
           explicitly allows draining to time out.
        3. Delete ONLY this account's credential from ``CredentialManager``.
        4. Remove ONLY this account's isolated runtime/profile directories,
           refusing any protected or GUI-owned profile via a hard guard.
        5. Leave every other account untouched.

        Emits ``ACCOUNT_REMOVE`` / ``ACCOUNT_REMOVE_FAILED`` audit entries and
        returns a secret-free :class:`RemovalResult`.
        """
        import time as _time

        from providers.registry.account_removal import (
            ProtectedProfileError,
            RemovalResult,
            _candidate_profile_dirs,
            assert_removable_profile,
            FROZEN_ACCOUNT_IDS,
        )

        auditor = self._audit()

        def _emit(action_attr: str, provider_id: str, from_state: Any, status: str, details: dict[str, Any] | None) -> None:
            if auditor is None:
                return
            try:
                from brain.governance.audit_logger import AccountAuditAction
                auditor.record(
                    action=getattr(AccountAuditAction, action_attr),
                    provider_id=provider_id,
                    account_id=account_id,
                    from_state=from_state,
                    correlation_id=corr,
                    status=status,
                    details=details,
                )
            except Exception:
                pass

        # Establish a stable correlation id for the whole flow.
        try:
            from brain.governance.audit_logger import new_correlation_id
            corr = correlation_id or new_correlation_id()
        except Exception:
            corr = correlation_id or account_id

        result = RemovalResult(account_id=account_id, correlation_id=corr)

        # Frozen accounts may never be removed.
        if account_id in FROZEN_ACCOUNT_IDS:
            result.error = f"Account '{account_id}' is frozen and cannot be removed"
            _emit("REMOVE_FAILED", "", None, "BLOCKED", {"reason": result.error})
            return result

        acc = self._accounts.get(account_id)

        # Idempotent: an absent account is a no-op success.
        if not acc:
            result.removed = True
            result.already_absent = True
            _emit("REMOVE", "", None, "SUCCESS", {"already_absent": True})
            return result

        result.provider_id = acc.provider_id
        from_state = acc.lifecycle_state

        # 1. Disable first so the router stops routing to it immediately.
        self.disable_account(account_id)

        # 2. Drain in-flight work rather than killing it.
        pool = self._pools.get(acc.provider_id)
        deadline = _time.time() + max(0.0, drain_timeout)
        while acc.current_concurrency > 0 and _time.time() < deadline:
            _time.sleep(0.05)
        result.drained_inflight = acc.current_concurrency
        if acc.current_concurrency > 0:
            # Refuse to hard-remove while work is genuinely in flight; the
            # account stays DISABLED (drained of new work) and is not deleted.
            result.error = (
                f"Account '{account_id}' still has {acc.current_concurrency} "
                f"in-flight task(s); left DISABLED and not removed"
            )
            _emit("REMOVE_FAILED", acc.provider_id, from_state, "WARNING",
                  {"in_flight": acc.current_concurrency})
            return result

        # 3. Remove ONLY this account's isolated directories, guarded hard.
        if remove_directories:
            for candidate in _candidate_profile_dirs(acc):
                try:
                    safe_path = assert_removable_profile(candidate)
                except ProtectedProfileError as exc:
                    # Hard stop: never delete a protected/GUI-owned profile.
                    result.error = str(exc)
                    _emit("REMOVE_FAILED", acc.provider_id, from_state, "BLOCKED",
                          {"protected_path": str(candidate)})
                    raise
                if safe_path.is_dir():
                    try:
                        shutil.rmtree(safe_path)
                        result.directories_removed.append(str(safe_path))
                    except Exception as exc:
                        result.directories_skipped.append(str(safe_path))
                        logger.debug("Could not remove account dir %s: %s", safe_path, exc)
                else:
                    result.directories_skipped.append(str(safe_path))

        # 4. Delete ONLY this account's credential.
        if acc.credential_reference:
            try:
                result.credential_deleted = bool(
                    get_credential_manager().delete(acc.credential_reference)
                )
            except Exception as exc:
                logger.debug("Credential delete failed for %s: %s", account_id, exc)

        # 5. Detach the account from the registry and its pool ONLY.
        self._accounts.pop(account_id, None)
        if pool:
            pool.remove_account(account_id)

        result.removed = True
        _emit("REMOVE", acc.provider_id, from_state, "SUCCESS", {
            "credential_deleted": result.credential_deleted,
            "directories_removed": len(result.directories_removed),
        })
        return result

    def update_account(
        self,
        account_id: str,
        display_name: str | None = None,
        priority: int | None = None,
        enabled: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        acc = self._accounts.get(account_id)
        if not acc:
            return False
        if display_name is not None:
            acc.display_name = display_name
        if priority is not None:
            acc.priority = priority
        if enabled is not None:
            acc.enabled = enabled
        if metadata is not None:
            acc.metadata.update(metadata)
        acc.updated_at = datetime.now(timezone.utc).isoformat()
        return True

    def rotate_pool(self, provider_id: str) -> list[str]:
        """Safely rotate accounts within a provider's pool."""
        pool = self._pools.get(provider_id)
        if not pool:
            return []
        return pool.rotate()

    def to_dict(self) -> dict[str, Any]:
        """Structured dictionary of all accounts grouped by provider."""
        out: dict[str, Any] = {}
        for p_id, pool in self._pools.items():
            out[p_id] = [a.to_dict() for a in pool.list_accounts()]
        return out
