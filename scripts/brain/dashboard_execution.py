"""Controlled local execution adapter for the Shared Brain dashboard.

This adapter is deliberately separate from the legacy swarm worker. It accepts
only an exact, explicitly approved planner output, launches only Kiro CLI with
a narrow named tool allowlist, and persists evidence for every lifecycle step.
It never invokes cloud CLIs, Antigravity, or --trust-all-tools. In-editor agents
(Cline, Antigravity IDE) are never executed here; they only receive a
durable delivery record that they must acknowledge themselves.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from .agent_adapters import AgentName, Availability
    from .execution import ApprovalRequiredError, ResourceLockManager, evaluate_execution, issue_approval
    from .model_policy import Action, Risk
    from .orchestrator import Agent, TaskPlan
    from . import swarm
except ImportError:
    from agent_adapters import AgentName, Availability
    from execution import ApprovalRequiredError, ResourceLockManager, evaluate_execution, issue_approval
    from model_policy import Action, Risk
    from orchestrator import Agent, TaskPlan
    import swarm

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_TIMEOUT_SECONDS = int(os.environ.get("BRAIN_DASHBOARD_EXECUTION_TIMEOUT_SECONDS", "300"))
# Kiro receives only repository read/write capabilities. It cannot receive a
# terminal, network, cloud, or unrestricted tool grant through this adapter.
TRUSTED_KIRO_TOOLS = "fs_read,fs_write"
# Planned agents that have no headless entrypoint. The dashboard may only record
# a durable delivery for them; it never claims they executed.
DELIVERY_PLAN_AGENTS = (Agent.CLINE, Agent.ANTIGRAVITY_IDE)


class ExecutionRejectedError(ValueError):
    """The requested plan is outside the dashboard's local execution policy."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, task: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(task, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _dashboard_availability(task) -> Availability:
    if task.agent is not Agent.KIRO:
        return Availability(None, False, None, None, "dashboard runner supports only kiro-cli")
    executable = shutil.which("kiro-cli")
    return Availability(
        AgentName.KIRO_CLI,
        bool(executable),
        "kiro-cli",
        executable,
        "dashboard local Kiro adapter found" if executable else "kiro-cli is not available on PATH",
    )


def _enforce_dashboard_scope(plan: TaskPlan) -> None:
    if not plan.tasks:
        raise ExecutionRejectedError("plan has no tasks")
    rejected: list[str] = []
    for task in plan.tasks:
        if task.agent not in {Agent.KIRO, *DELIVERY_PLAN_AGENTS}:
            rejected.append(f"{task.id}: planned agent {task.agent.value} has no dashboard runtime adapter")
        if task.action not in {Action.PLAN, Action.ANALYZE, Action.IMPLEMENT}:
            rejected.append(f"{task.id}: action {task.action.value} is outside the local execution allowlist")
        if task.risk not in {Risk.LOW, Risk.MEDIUM}:
            rejected.append(f"{task.id}: risk {task.risk.value} requires a separately scoped approval")
        if "cloud" in task.locks:
            rejected.append(f"{task.id}: cloud-scoped work is not permitted from the dashboard")
    if rejected:
        raise ExecutionRejectedError("; ".join(rejected))


def _task_record(task, *, batch_id: str, approval_id: str) -> dict:
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    timestamp = _now()
    return {
        "id": task_id,
        "title": task.title,
        "description": task.rationale,
        "assigned_to": "kiro-cli",
        "planned_agent": task.agent.value,
        "action": task.action.value,
        "risk": task.risk.value,
        "status": "pending",
        "stage_state": "approved_for_local_execution",
        "batch_id": batch_id,
        "plan_hash": None,
        "approval_id": approval_id,
        "created_at": timestamp,
        "updated_at": timestamp,
        "started_at": None,
        "heartbeat_at": None,
        "completed_at": None,
        "output": None,
        "error": None,
        "duration_seconds": None,
        "verification": None,
    }


class DashboardExecutor:
    """Starts one sequential, approval-bound batch in a background thread."""

    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        executable_lookup: Callable[[str], str | None] = shutil.which,
        timeout_seconds: int = EXECUTION_TIMEOUT_SECONDS,
    ) -> None:
        self.runner = runner or subprocess.run
        self.executable_lookup = executable_lookup
        self.timeout_seconds = timeout_seconds
        self.lock_manager = ResourceLockManager(swarm.SWARM_DIR / "locks")
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()

    def submit(self, plan: TaskPlan, *, plan_hash: str, explicit_confirmation: bool) -> dict:
        if plan_hash != plan.plan_hash:
            raise ExecutionRejectedError("submitted plan hash does not match the current task text")
        _enforce_dashboard_scope(plan)
        try:
            approval = issue_approval(plan.plan_hash, "dashboard-local-user", explicit_confirmation=explicit_confirmation)
        except ApprovalRequiredError as error:
            raise ExecutionRejectedError(str(error)) from error
        delivery_agents = {task.agent for task in plan.tasks if task.agent in DELIVERY_PLAN_AGENTS}
        if delivery_agents:
            if len(delivery_agents) > 1 or any(task.agent not in DELIVERY_PLAN_AGENTS for task in plan.tasks):
                raise ExecutionRejectedError("plans mixing Kiro and in-editor delivery agents must be submitted separately so each runtime has one durable lifecycle")
            return self._stage_delivery_plan(plan, approval.id, delivery_agents.pop())
        receipt = evaluate_execution(plan, approval, availability_resolver=_dashboard_availability, lock_manager=self.lock_manager)
        if receipt.status != "authorized-dry-run":
            raise ExecutionRejectedError(receipt.reason)

        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        records = [_task_record(task, batch_id=batch_id, approval_id=approval.id) for task in plan.tasks]
        for record in records:
            record["plan_hash"] = plan.plan_hash
            _atomic_write(swarm.PENDING_DIR / f"{record['id']}.json", record)
        thread = threading.Thread(target=self._run_batch, args=(plan, records), name=batch_id, daemon=True)
        with self._guard:
            self._threads[batch_id] = thread
        thread.start()
        return {
            "status": "accepted",
            "batch_id": batch_id,
            "plan_hash": plan.plan_hash,
            "approval_id": approval.id,
            "tasks": [{"id": record["id"], "title": record["title"], "status": record["status"]} for record in records],
            "policy": "local Kiro execution; read/write tools only; no terminal, cloud, network, delete, or deployment permissions",
        }

    def _stage_delivery_plan(self, plan: TaskPlan, approval_id: str, agent: Agent) -> dict:
        """Record a durable delivery for an in-editor agent; never claim execution."""
        name = agent.value
        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        staged: list[dict] = []
        for planned_task in plan.tasks:
            task = swarm.create_task(planned_task.title, planned_task.rationale, preferred_agent=name)
            pending_path = swarm.PENDING_DIR / f"{task['id']}.json"
            task.update({"batch_id": batch_id, "plan_hash": plan.plan_hash, "approval_id": approval_id, "action": planned_task.action.value, "risk": planned_task.risk.value, "stage_state": f"approved_for_{name}_delivery", "updated_at": _now()})
            _atomic_write(pending_path, task)
            output, error = swarm.stage_task_for_delivery(task, name)
            if error:
                raise ExecutionRejectedError(error)
            staged.append({"id": task["id"], "title": task["title"], "status": f"staged_for_{name}", "delivery": output})
        return {
            "status": f"staged_for_{name}",
            "agent": name,
            "batch_id": batch_id,
            "plan_hash": plan.plan_hash,
            "approval_id": approval_id,
            "tasks": staged,
            "policy": f"{name} task delivery recorded; no headless {name} execution is claimed. {name} must acknowledge and report progress.",
        }

    def _stage_cline_plan(self, plan: TaskPlan, approval_id: str) -> dict:
        """Backward-compatible entrypoint for Cline-only delivery plans."""
        return self._stage_delivery_plan(plan, approval_id, Agent.CLINE)

    def _run_batch(self, plan: TaskPlan, records: list[dict]) -> None:
        try:
            for planned_task, record in zip(plan.tasks, records, strict=True):
                self._run_task(planned_task, record)
        finally:
            with self._guard:
                self._threads.pop(records[0]["batch_id"], None)

    def _run_task(self, planned_task, record: dict) -> None:
        pending_path = swarm.PENDING_DIR / f"{record['id']}.json"
        progress_path = swarm.IN_PROGRESS_DIR / f"{record['id']}.json"
        record.update({"status": "in-progress", "stage_state": "kiro_local_runner_started", "started_at": _now(), "heartbeat_at": _now(), "updated_at": _now()})
        _atomic_write(progress_path, record)
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass

        started = time.monotonic()
        leases = ()
        try:
            leases = self.lock_manager.acquire(planned_task.locks, record["id"])
            executable = self.executable_lookup("kiro-cli")
            if not executable:
                raise RuntimeError("kiro-cli is not available on PATH")
            prompt = (
                "You are executing one locally approved Shared Brain dashboard task. "
                f"Work only inside {REPO_ROOT}. Do not use cloud, network, package installation, deletion, deployment, "
                "or any terminal command. Use only the explicitly trusted repository read/write tools. "
                "State exactly what you changed or why you could not complete it.\n\n"
                f"Task: {planned_task.title}\n"
                f"Required verification: {', '.join(planned_task.verification)}"
            )
            command = [executable, "chat", "--no-interactive", f"--trust-tools={TRUSTED_KIRO_TOOLS}", prompt]
            completed = self.runner(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd=str(REPO_ROOT),
                env=os.environ | {"BRAIN_DASHBOARD_TASK_ID": record["id"]},
            )
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            record["completed_at"] = _now()
            record["updated_at"] = _now()
            record["heartbeat_at"] = _now()
            record["output"] = (completed.stdout or "").strip()
            if completed.returncode == 0:
                record.update({
                    "status": "completed",
                    "stage_state": "local_runner_finished",
                    "verification": "Kiro process exited 0; output captured. Review the recorded output and repository diff before merging.",
                })
                destination = swarm.COMPLETED_DIR / f"{record['id']}.json"
            else:
                record.update({"status": "escalated", "stage_state": "local_runner_failed", "error": (completed.stderr or f"kiro-cli exited {completed.returncode}").strip()})
                destination = swarm.ESCALATED_DIR / f"{record['id']}.json"
        except subprocess.TimeoutExpired:
            record.update({"status": "escalated", "stage_state": "local_runner_timed_out", "error": f"local Kiro execution timed out after {self.timeout_seconds} seconds", "duration_seconds": round(time.monotonic() - started, 3), "updated_at": _now(), "heartbeat_at": _now()})
            destination = swarm.ESCALATED_DIR / f"{record['id']}.json"
        except Exception as error:
            record.update({"status": "escalated", "stage_state": "local_runner_blocked", "error": str(error), "duration_seconds": round(time.monotonic() - started, 3), "updated_at": _now(), "heartbeat_at": _now()})
            destination = swarm.ESCALATED_DIR / f"{record['id']}.json"
        finally:
            self.lock_manager.release(leases)
        try:
            progress_path.unlink()
        except FileNotFoundError:
            pass
        _atomic_write(destination, record)
