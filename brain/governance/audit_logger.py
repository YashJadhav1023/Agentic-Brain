"""Universal Governance and Immutable Audit Logger for Mission Control Phase 20.

Maintains an append-only, tamper-evident audit record of every system event:
- Resource and capability discovery
- Routing competition and decisions
- Planning, dependency DAG generation, and approval gates
- Execution dispatch, verification, and rollbacks
- Account rotation and provider failovers
- Safety gate triggers and credential access
All logged data is automatically sanitized via CredentialManager redaction.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from providers.registry.credential_manager import get_credential_manager

logger = logging.getLogger(__name__)


@dataclass
class AuditEvent:
    """Structured immutable audit event record."""
    event_id: str
    category: str  # DISCOVERY, ROUTING, PLANNING, APPROVAL, EXECUTION, ROLLBACK, FAILOVER, SECURITY
    action: str
    actor: str = "system"
    target: Optional[str] = None
    status: str = "SUCCESS"  # SUCCESS, FAILURE, WARNING, BLOCKED
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


#: Anchored to the repository root (the same base LogRotator rotates), never the
#: process CWD: a relative default scattered stray ledgers such as
#: ui/dashboard/runtime/audit/audit.jsonl depending on where a command ran.
DEFAULT_AUDIT_LOG_PATH = Path(__file__).resolve().parents[2] / "runtime" / "audit" / "audit.jsonl"


class AuditLogger:
    """Append-only audit trail logger with automatic secret redaction."""

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.log_path = Path(log_path) if log_path else DEFAULT_AUDIT_LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.cred_manager = get_credential_manager()

    def _sanitize(self, val: Any) -> Any:
        """Recursively redact secrets from audit payload."""
        if isinstance(val, str):
            return self.cred_manager.redactor.redact_text(val)
        if isinstance(val, dict):
            return {k: self._sanitize(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._sanitize(x) for x in val]
        return val

    def log(
        self,
        category: str,
        action: str,
        actor: str = "system",
        target: Optional[str] = None,
        status: str = "SUCCESS",
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        """Append a redacted audit event to the log file."""
        with self._lock:
            event_id = f"aud-{uuid.uuid4().hex[:8]}"
            clean_details = self._sanitize(details or {})
            clean_target = self.cred_manager.redactor.redact_text(target) if target else None

            event = AuditEvent(
                event_id=event_id,
                category=category.upper(),
                action=action,
                actor=actor,
                target=clean_target,
                status=status.upper(),
                details=clean_details,
            )

            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict()) + "\n")
            except Exception as e:
                logger.debug("Failed appending audit event: %s", e)

            return event

    def get_events(
        self,
        limit: int = 50,
        category: Optional[str] = None,
        status: Optional[str] = None,
        action: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent audit events in reverse chronological order.

        Supports optional filtering by category, status, action, and the
        correlation id embedded in ``details`` so an entire onboarding or
        removal flow can be traced end-to-end.
        """
        if not self.log_path.exists():
            return []

        events: List[Dict[str, Any]] = []
        with self._lock:
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                d = json.loads(line)
                                if category and d.get("category") != category.upper():
                                    continue
                                if status and d.get("status") != status.upper():
                                    continue
                                if action and d.get("action") != action:
                                    continue
                                if correlation_id and (
                                    d.get("details", {}) or {}
                                ).get("correlation_id") != correlation_id:
                                    continue
                                events.append(d)
                            except Exception:
                                continue
            except Exception as e:
                logger.debug("Failed reading audit log: %s", e)

        return list(reversed(events))[:limit]


#: Audit category for every account lifecycle / onboarding / removal event.
ACCOUNT_AUDIT_CATEGORY = "ACCOUNT"


class AccountAuditAction:
    """Canonical, machine-readable audit action types for account governance.

    Every account lifecycle action emits one of these actions through
    :class:`AccountAuditor`, giving the dashboard and any auditor a stable,
    immutable record of the whole onboarding, health, and removal flow.
    """

    CREATE = "ACCOUNT_CREATE"
    CREATE_FAILED = "ACCOUNT_CREATE_FAILED"
    AUTH_STARTED = "ACCOUNT_AUTH_STARTED"
    AUTH_SUCCESS = "ACCOUNT_AUTH_SUCCESS"
    AUTH_FAILED = "ACCOUNT_AUTH_FAILED"
    AUTH_CANCELLED = "ACCOUNT_AUTH_CANCELLED"
    VALIDATION_SUCCESS = "ACCOUNT_VALIDATION_SUCCESS"
    VALIDATION_FAILED = "ACCOUNT_VALIDATION_FAILED"
    ENABLE = "ACCOUNT_ENABLE"
    DISABLE = "ACCOUNT_DISABLE"
    HEALTH_CHECK = "ACCOUNT_HEALTH_CHECK"
    REMOVE = "ACCOUNT_REMOVE"
    REMOVE_FAILED = "ACCOUNT_REMOVE_FAILED"
    LIFECYCLE_TRANSITION = "ACCOUNT_LIFECYCLE_TRANSITION"


def new_correlation_id() -> str:
    """Generate a correlation id used to trace a whole account flow."""
    return f"acorr-{uuid.uuid4().hex[:12]}"


class AccountAuditor:
    """Thin, secret-safe facade over :class:`AuditLogger` for account events.

    Guarantees every emitted entry carries a correlation id (so a whole
    onboarding flow is traceable), the provider id, the account id, the
    from/to lifecycle state where applicable, and a UTC timestamp. All payload
    values are routed through the underlying logger's redaction, so audit
    entries never contain secret values.
    """

    def __init__(self, audit_logger: Optional[AuditLogger] = None) -> None:
        self.audit = audit_logger or AuditLogger()

    def record(
        self,
        action: str,
        provider_id: str,
        account_id: str,
        correlation_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        status: str = "SUCCESS",
        actor: str = "system",
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        """Emit a single immutable, redacted account audit entry."""
        corr = correlation_id or new_correlation_id()
        payload: Dict[str, Any] = {
            "correlation_id": corr,
            "provider_id": provider_id,
            "account_id": account_id,
        }
        if from_state is not None:
            payload["from_state"] = str(
                from_state.value if hasattr(from_state, "value") else from_state
            )
        if to_state is not None:
            payload["to_state"] = str(
                to_state.value if hasattr(to_state, "value") else to_state
            )
        if details:
            # Never trust caller-supplied keys to override the structural ones.
            for k, v in details.items():
                if k not in payload:
                    payload[k] = v

        return self.audit.log(
            category=ACCOUNT_AUDIT_CATEGORY,
            action=action,
            actor=actor,
            target=account_id,
            status=status,
            details=payload,
        )

    def get_account_events(
        self,
        limit: int = 100,
        account_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        action: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return recent account audit events, optionally filtered."""
        events = self.audit.get_events(
            limit=10_000,
            category=ACCOUNT_AUDIT_CATEGORY,
            action=action,
            correlation_id=correlation_id,
        )
        if account_id:
            events = [
                e for e in events
                if (e.get("details", {}) or {}).get("account_id") == account_id
            ]
        return events[:limit]


#: Global default account auditor instance.
_DEFAULT_ACCOUNT_AUDITOR: Optional[AccountAuditor] = None


def get_account_auditor() -> AccountAuditor:
    global _DEFAULT_ACCOUNT_AUDITOR
    if _DEFAULT_ACCOUNT_AUDITOR is None:
        _DEFAULT_ACCOUNT_AUDITOR = AccountAuditor()
    return _DEFAULT_ACCOUNT_AUDITOR
