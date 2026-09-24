"""Approval-gated, non-executing orchestration primitives for Shared Brain.

This module validates a plan before any potential execution boundary.  It does
not invoke agents, subprocesses, cloud APIs, or note mutations.  Even an
approved request only receives a dry-run receipt while execution is disabled.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

try:
    from .agent_adapters import Availability, Eligibility, evaluate_eligibility
    from .orchestrator import PlannedTask, TaskPlan
except ImportError:
    from agent_adapters import Availability, Eligibility, evaluate_eligibility
    from orchestrator import PlannedTask, TaskPlan

APPROVAL_TTL_SECONDS = 15 * 60
_LOCK_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class ApprovalRequiredError(ValueError):
    pass


class PlanIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class Approval:
    id: str
    plan_hash: str
    approved_by: str
    issued_at: float
    expires_at: float

    def valid_for(self, plan_hash: str, now: float | None = None) -> bool:
        return self.plan_hash == plan_hash and (now if now is not None else time.time()) < self.expires_at


@dataclass(frozen=True)
class LockLease:
    resource: str
    owner: str
    path: Path


class ResourceLockManager:
    """Filesystem lock manager; callers must explicitly release acquired leases."""

    def __init__(self, root: Path):
        self.root = root

    def acquire(self, resources: Iterable[str], owner: str) -> tuple[LockLease, ...]:
        if not owner.strip():
            raise ValueError("lock owner is required")
        names = tuple(sorted(set(resources)))
        if any(_LOCK_NAME.fullmatch(name) is None for name in names):
            raise ValueError("resource lock names must be lowercase safe identifiers")
        self.root.mkdir(parents=True, exist_ok=True)
        leases: list[LockLease] = []
        try:
            for name in names:
                path = self.root / f"{name}.lock"
                try:
                    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError as exc:
                    raise RuntimeError(f"resource already locked: {name}") from exc
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"resource": name, "owner": owner, "issued_at": time.time()}) + "\n")
                leases.append(LockLease(name, owner, path))
            return tuple(leases)
        except Exception:
            self.release(leases)
            raise

    def release(self, leases: Iterable[LockLease]) -> None:
        for lease in leases:
            try:
                lease.path.unlink()
            except FileNotFoundError:
                pass

    def conflicts(self, resources: Iterable[str]) -> tuple[str, ...]:
        names = tuple(sorted(set(resources)))
        if any(_LOCK_NAME.fullmatch(name) is None for name in names):
            raise ValueError("resource lock names must be lowercase safe identifiers")
        return tuple(name for name in names if (self.root / f"{name}.lock").exists())


def canonical_plan_hash(plan: TaskPlan) -> str:
    """Recompute the planner-compatible hash to reject altered plan objects."""
    tasks = [
        asdict(task) | {
            "agent": task.agent.value,
            "action": task.action.value,
            "risk": task.risk.value,
            "complexity": task.complexity.value,
        }
        for task in plan.tasks
    ]
    payload = json.dumps(tasks, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def issue_approval(plan_hash: str, approved_by: str, *, explicit_confirmation: bool, now: float | None = None) -> Approval:
    if explicit_confirmation is not True:
        raise ApprovalRequiredError("approval requires explicit_confirmation=True")
    if not re.fullmatch(r"[0-9a-f]{16}", plan_hash):
        raise ApprovalRequiredError("approval requires a valid plan hash")
    if not approved_by or not approved_by.strip():
        raise ApprovalRequiredError("approval requires a non-empty approver")
    issued = time.time() if now is None else now
    return Approval(uuid.uuid4().hex, plan_hash, approved_by.strip(), issued, issued + APPROVAL_TTL_SECONDS)


@dataclass(frozen=True)
class VerificationReceipt:
    id: str
    plan_hash: str
    status: str
    executed: bool
    verification: str
    timestamp: float
    eligibility: tuple[Eligibility, ...]
    reason: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "plan_hash": self.plan_hash,
            "status": self.status,
            "executed": self.executed,
            "verification": self.verification,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "eligibility": [
                {
                    "agent": item.agent.value if item.agent else None,
                    "action": item.action.value if item.action else None,
                    "risk": item.risk.value if item.risk else None,
                    "eligible": item.eligible,
                    "reason": item.reason,
                    "available": item.availability.available,
                }
                for item in self.eligibility
            ],
        }


AvailabilityResolver = Callable[[PlannedTask], Availability | None]


def _receipt(plan: TaskPlan, status: str, reason: str, eligibility: tuple[Eligibility, ...] = ()) -> VerificationReceipt:
    return VerificationReceipt(uuid.uuid4().hex, plan.plan_hash, status, False, "not-executed", time.time(), eligibility, reason)


def evaluate_execution(
    plan: TaskPlan,
    approval: Approval | None,
    *,
    availability_resolver: AvailabilityResolver | None = None,
    lock_manager: ResourceLockManager | None = None,
    now: float | None = None,
) -> VerificationReceipt:
    """Validate an execution request but never launch work.

    Successful validation receives an ``authorized-dry-run`` receipt.  A real
    executor must be introduced separately; this phase deliberately cannot run
    agents even with an approval.
    """
    recomputed = canonical_plan_hash(plan)
    if plan.plan_hash != recomputed:
        raise PlanIntegrityError("plan hash does not match its task payload")
    if approval is None or not approval.valid_for(plan.plan_hash, now):
        return _receipt(plan, "blocked", "valid unexpired approval for this exact plan is required")
    required_locks = tuple(sorted({lock for task in plan.tasks for lock in task.locks}))
    if lock_manager is not None:
        conflicts = lock_manager.conflicts(required_locks)
        if conflicts:
            return _receipt(plan, "blocked", f"resource lock conflict: {', '.join(conflicts)}")
    decisions: list[Eligibility] = []
    for task in plan.tasks:
        availability = availability_resolver(task) if availability_resolver else None
        decision = evaluate_eligibility(task.agent.value, task.action, task.risk, availability=availability)
        decisions.append(decision)
    immutable_decisions = tuple(decisions)
    rejected = next((item for item in immutable_decisions if not item.eligible), None)
    if rejected:
        return _receipt(plan, "blocked", f"eligibility rejected: {rejected.reason}", immutable_decisions)
    return _receipt(plan, "authorized-dry-run", "approval and eligibility validated; execution subsystem intentionally disabled", immutable_decisions)
