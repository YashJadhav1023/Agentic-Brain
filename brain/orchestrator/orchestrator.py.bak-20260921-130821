"""Master Orchestrator.

High-level interface for routing user instructions to agents, persisting them as
tasks, dispatching them to the bounded swarm pool, and resuming work.

The orchestrator is provider-agnostic: it talks to the ProviderRegistry and the
AgentAdapter interface only, so adding a future provider requires no change
here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from brain.context.continuator import ContinueContext, UniversalContinuator
from brain.orchestrator.job import Job
from brain.orchestrator.job_manager import JobManager
from brain.orchestrator.swarm import SwarmWorkerPool
from brain.router.smart_router import SmartRouter
from brain.worktree.worktree_manager import WorktreeManager
from events.bus import EventBus, EventType
from handoffs.handoff_manager import HandoffManager
from memory.store.memory_store import MemoryStore
from providers.registry.bootstrap import create_default_registry
from providers.registry.provider_registry import ProviderRegistry
from sessions.session_manager import SessionManager
from tasks.manager import Task, TaskManager, TaskPriority, TaskStatus

# BUG-002 fix (full-system validation): destructive instructions queued through
# plan_and_dispatch must never auto-execute from the shared swarm queue. The
# same verb classes used by the Phase 18 TaskDecomposer DESTRUCTIVE_PATTERN are
# stamped onto the task here and enforced again at queue pickup (swarm).
DESTRUCTIVE_INSTRUCTION_PATTERN = re.compile(
    r"\b(delete|destroy|drop|purge|rm\s+-rf|truncate|erase|wipe|kill)\b",
    re.IGNORECASE,
)


class Orchestrator:
    """End-to-end orchestration coordinator."""

    def __init__(
        self,
        task_manager: TaskManager | None = None,
        registry: ProviderRegistry | None = None,
        event_bus: EventBus | None = None,
        workspace_dir: Path | None = None,
        handoff_manager: HandoffManager | None = None,
        session_manager: SessionManager | None = None,
        memory_store: MemoryStore | None = None,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self._workspace = workspace_dir or Path.cwd()
        self._registry = registry or create_default_registry()
        self._task_manager = task_manager or TaskManager(
            root_tasks_dir=self._workspace / "tasks"
        )
        self._event_bus = event_bus or EventBus()
        self._handoff_manager = handoff_manager or HandoffManager(
            root_dir=self._workspace / "handoffs"
        )
        self._session_manager = session_manager or SessionManager()
        self._memory_store = memory_store or MemoryStore()
        self._worktree_manager = worktree_manager or WorktreeManager(canonical_repo=self._workspace)
        self._router = SmartRouter(self._registry, task_manager=self._task_manager)
        self._swarm = SwarmWorkerPool(
            task_manager=self._task_manager,
            registry=self._registry,
            event_bus=self._event_bus,
            handoff_manager=self._handoff_manager,
            memory_store=self._memory_store,
            session_manager=self._session_manager,
            workspace_dir=self._workspace,
            worktree_manager=self._worktree_manager,
        )
        self._job_manager = JobManager(
            provider_registry=self._registry,
            storage_dir=self._workspace / "tasks" / "jobs",
        )

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def jobs(self) -> JobManager:
        return self._job_manager

    @property
    def worktrees(self) -> WorktreeManager:
        return self._worktree_manager

    @property
    def registry(self) -> ProviderRegistry:
        return self._registry

    @property
    def router(self) -> SmartRouter:
        return self._router

    @property
    def tasks(self) -> TaskManager:
        return self._task_manager

    @property
    def swarm(self) -> SwarmWorkerPool:
        return self._swarm

    @property
    def sessions(self) -> SessionManager:
        return self._session_manager

    # ------------------------------------------------------------------
    # Planning and execution
    # ------------------------------------------------------------------
    def plan_and_dispatch(
        self,
        instruction: str,
        files: list[str] | None = None,
        priority: TaskPriority = TaskPriority.NORMAL,
        preferred_agent: str | None = None,
        preferred_model: str | None = None,
        execution_options: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        task_type: str = "general",
        parent_task: str | None = None,
        continuation_depth: int = 0,
        max_continuation_depth: int = 5,
        verification_depth: int = 0,
        max_verification_depth: int = 1,
        continuation_budget: int = 5,
        verification_for: str | None = None,
    ) -> Task:
        """Route an instruction, create a persistent task and queue it."""
        # Detect verification intent
        is_verif = (
            task_type == "verification"
            or bool(verification_for)
            or instruction.lower().startswith("verify the output of task")
            or instruction.lower().startswith("verify output of task")
        )
        if is_verif:
            task_type = "verification"
            if not verification_for:
                match = re.search(
                    r"verify (?:the )?output of task ([a-zA-Z0-9_-]+)",
                    instruction,
                    re.IGNORECASE,
                )
                if match:
                    verification_for = match.group(1)

            # 1. Prevention of Verification-of-Verification recursion
            if verification_for:
                target = self._task_manager.get_task(verification_for)
                if target and (target.is_verification or target.task_type == "verification"):
                    self._event_bus.publish(
                        EventType.RECURSION_PREVENTED,
                        metadata={"target_task": verification_for, "instruction": instruction},
                    )
                    self._event_bus.publish(
                        EventType.VERIFICATION_REJECTED,
                        metadata={"reason": "Cannot verify a verification task", "target_task": verification_for},
                    )
                    self._event_bus.publish(
                        EventType.CONTINUATION_TERMINATED,
                        metadata={"reason": "RECURSION_PREVENTED"},
                    )
                    task = self._task_manager.create_task(
                        title=instruction[:80],
                        description=instruction,
                        priority=priority,
                        task_type="verification",
                        parent_task=parent_task,
                        continuation_depth=continuation_depth,
                        max_continuation_depth=max_continuation_depth,
                        verification_depth=verification_depth,
                        max_verification_depth=max_verification_depth,
                        continuation_budget=continuation_budget,
                        verification_for=verification_for,
                        is_terminal=True,
                        terminal_reason="RECURSION_PREVENTED",
                    )
                    task = self._task_manager.update_status(
                        task.task_id,
                        TaskStatus.REJECTED,
                        is_terminal=True,
                        terminal_reason="RECURSION_PREVENTED",
                        error="Recursive verification is prohibited",
                    ) or task
                    return task

                # 2. Duplicate verification prevention (Idempotency)
                existing_verifs = [
                    t for t in self._task_manager.list_tasks()
                    if (
                        t.verification_for == verification_for
                        or (t.task_type == "verification" and verification_for in t.description)
                    )
                    and t.status in (
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.COMPLETED,
                        TaskStatus.VERIFICATION_COMPLETE,
                    )
                ]
                if existing_verifs:
                    self._event_bus.publish(
                        EventType.VERIFICATION_REJECTED,
                        metadata={"reason": "Duplicate verification prevented", "target_task": verification_for},
                    )
                    self._event_bus.publish(
                        EventType.RECURSION_PREVENTED,
                        metadata={"reason": "Duplicate verification prevented", "target_task": verification_for},
                    )
                    self._event_bus.publish(
                        EventType.CONTINUATION_TERMINATED,
                        metadata={"reason": "DUPLICATE_VERIFICATION"},
                    )
                    task = self._task_manager.create_task(
                        title=instruction[:80],
                        description=instruction,
                        priority=priority,
                        task_type="verification",
                        parent_task=parent_task,
                        continuation_depth=continuation_depth,
                        max_continuation_depth=max_continuation_depth,
                        verification_depth=verification_depth,
                        max_verification_depth=max_verification_depth,
                        continuation_budget=continuation_budget,
                        verification_for=verification_for,
                        is_terminal=True,
                        terminal_reason="DUPLICATE_VERIFICATION",
                    )
                    task = self._task_manager.update_status(
                        task.task_id,
                        TaskStatus.REJECTED,
                        is_terminal=True,
                        terminal_reason="DUPLICATE_VERIFICATION",
                        error=f"Duplicate verification for task {verification_for} is prohibited",
                    ) or task
                    return task

            # 3. Verification depth check
            if verification_depth > max_verification_depth:
                self._event_bus.publish(
                    EventType.RECURSION_PREVENTED,
                    metadata={"verification_depth": verification_depth, "max": max_verification_depth},
                )
                self._event_bus.publish(
                    EventType.DEPTH_LIMIT_REACHED,
                    metadata={"verification_depth": verification_depth, "max": max_verification_depth},
                )
                self._event_bus.publish(
                    EventType.CONTINUATION_TERMINATED,
                    metadata={"reason": "DEPTH_LIMIT_REACHED"},
                )
                task = self._task_manager.create_task(
                    title=instruction[:80],
                    description=instruction,
                    priority=priority,
                    task_type=task_type,
                    parent_task=parent_task,
                    continuation_depth=continuation_depth,
                    max_continuation_depth=max_continuation_depth,
                    verification_depth=verification_depth,
                    max_verification_depth=max_verification_depth,
                    continuation_budget=continuation_budget,
                    verification_for=verification_for,
                    is_terminal=True,
                    terminal_reason="DEPTH_LIMIT_REACHED",
                )
                task = self._task_manager.update_status(
                    task.task_id,
                    TaskStatus.DEPTH_LIMIT_REACHED,
                    is_terminal=True,
                    terminal_reason="DEPTH_LIMIT_REACHED",
                    error=f"Verification depth limit ({max_verification_depth}) exceeded",
                ) or task
                return task

        # 4. Continuation depth and budget limits check
        if continuation_depth > max_continuation_depth:
            self._event_bus.publish(
                EventType.DEPTH_LIMIT_REACHED,
                metadata={"continuation_depth": continuation_depth, "max": max_continuation_depth},
            )
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                metadata={"reason": "DEPTH_LIMIT_REACHED"},
            )
            task = self._task_manager.create_task(
                title=instruction[:80],
                description=instruction,
                priority=priority,
                task_type=task_type,
                parent_task=parent_task,
                continuation_depth=continuation_depth,
                max_continuation_depth=max_continuation_depth,
                verification_depth=verification_depth,
                max_verification_depth=max_verification_depth,
                continuation_budget=continuation_budget,
                verification_for=verification_for,
                is_terminal=True,
                terminal_reason="DEPTH_LIMIT_REACHED",
            )
            task = self._task_manager.update_status(
                task.task_id,
                TaskStatus.DEPTH_LIMIT_REACHED,
                is_terminal=True,
                terminal_reason="DEPTH_LIMIT_REACHED",
                error=f"Continuation depth limit ({max_continuation_depth}) exceeded",
            ) or task
            return task

        if continuation_budget <= 0:
            self._event_bus.publish(
                EventType.BUDGET_EXHAUSTED,
                metadata={"continuation_budget": continuation_budget},
            )
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                metadata={"reason": "BUDGET_EXHAUSTED"},
            )
            task = self._task_manager.create_task(
                title=instruction[:80],
                description=instruction,
                priority=priority,
                task_type=task_type,
                parent_task=parent_task,
                continuation_depth=continuation_depth,
                max_continuation_depth=max_continuation_depth,
                verification_depth=verification_depth,
                max_verification_depth=max_verification_depth,
                continuation_budget=continuation_budget,
                verification_for=verification_for,
                is_terminal=True,
                terminal_reason="BUDGET_EXHAUSTED",
            )
            task = self._task_manager.update_status(
                task.task_id,
                TaskStatus.BUDGET_EXHAUSTED,
                is_terminal=True,
                terminal_reason="BUDGET_EXHAUSTED",
                error="Continuation budget exhausted",
            ) or task
            return task

        self._event_bus.publish(
            EventType.ROUTE_STARTED,
            metadata={"instruction": instruction[:200], "preferred_agent": preferred_agent},
        )
        decision = self._router.route(
            task_text=instruction,
            preferred_agent=preferred_agent,
            preferred_model=preferred_model,
        )
        self._event_bus.publish(
            EventType.ROUTE_SELECTED,
            agent_id=decision.agent_id,
            provider=decision.provider,
            metadata={
                "assigned_model": decision.model,
                "complexity": decision.complexity.value,
                "reason": decision.reason,
                "fallback_agent": decision.fallback_agent_id,
            },
        )

        task = self._task_manager.create_task(
            title=instruction[:80],
            description=instruction,
            priority=priority,
            complexity=decision.complexity.value,
            assigned_agent=decision.agent_id,
            assigned_account=decision.account_id,
            assigned_model=decision.model,
            task_type=task_type,
            parent_task=parent_task,
            continuation_depth=continuation_depth,
            max_continuation_depth=max_continuation_depth,
            verification_depth=verification_depth,
            max_verification_depth=max_verification_depth,
            continuation_budget=continuation_budget,
            verification_for=verification_for,
        )
        task.requested_model = decision.model
        if files:
            task.files = files
        if execution_options:
            task.execution_options = dict(execution_options)
        if conversation_id:
            task.conversation_id = conversation_id
        # BUG-002 fix: stamp the Phase 18 approval gate onto the queued task so
        # the swarm (and any other queue consumer) cannot auto-execute it.
        if DESTRUCTIVE_INSTRUCTION_PATTERN.search(instruction):
            task.requires_approval = True
            task.approval_reason = (
                "Instruction matches destructive operation pattern; "
                "explicit operator approval required before execution"
            )
        self._task_manager.save_task(task)

        self._event_bus.publish(
            EventType.TASK_CREATED,
            agent_id=decision.agent_id,
            task_id=task.task_id,
            metadata={
                "account_id": decision.account_id,
                "provider": decision.provider,
                "assigned_model": decision.model,
                "complexity": decision.complexity.value,
                "routing_reason": decision.reason,
                "required_capabilities": decision.required_capabilities,
                "fallback_agent": decision.fallback_agent_id,
                "task_type": task_type,
                "continuation_depth": continuation_depth,
                "requires_approval": task.requires_approval,
            },
        )
        self._event_bus.publish(
            EventType.TASK_ASSIGNED,
            agent_id=decision.agent_id,
            task_id=task.task_id,
            metadata={"account_id": decision.account_id, "model": decision.model},
        )
        self._event_bus.publish(
            EventType.TASK_QUEUED,
            agent_id=decision.agent_id,
            task_id=task.task_id,
            provider=decision.provider,
            metadata={"stage": "QUEUED"},
        )

        return task

    def approve_task(self, task_id: str, approver: str = "operator") -> Task | None:
        """Explicitly approve a gated task, releasing it to the READY queue.

        BUG-002 fix companion API: destructive instructions are queued in
        BLOCKED_ON_APPROVAL and can only reach the swarm through this method.
        """
        task = self._task_manager.get_task(task_id)
        if not task:
            return None
        if not task.requires_approval:
            return task
        task.requires_approval = False
        task.approval_reason = f"approved by {approver}"
        # Persist the flag change first: update_status() re-reads the record
        # from disk, so in-memory mutations alone would be discarded.
        self._task_manager.save_task(task)
        self._task_manager.update_status(task.task_id, TaskStatus.READY, stage="QUEUED")
        self._event_bus.publish(
            EventType.TASK_RECONCILED,
            agent_id=task.assigned_agent,
            task_id=task.task_id,
            metadata={
                "action": "APPROVAL_GRANTED",
                "approver": approver,
                "approval_reason": task.approval_reason,
            },
        )
        return self._task_manager.get_task(task_id)

    def reject_task(self, task_id: str, reason: str = "rejected by operator") -> Task | None:
        """Reject a gated (or any queued) task, terminating it without execution."""
        task = self._task_manager.get_task(task_id)
        if not task:
            return None
        task.requires_approval = False
        task.approval_reason = reason
        self._task_manager.save_task(task)
        updated = self._task_manager.update_status(
            task.task_id,
            TaskStatus.REJECTED,
            is_terminal=True,
            terminal_reason="REJECTED",
            error=f"Task rejected without execution: {reason}",
            stage="FAILED",
        )
        self._event_bus.publish(
            EventType.TASK_RECONCILED,
            agent_id=task.assigned_agent,
            task_id=task.task_id,
            metadata={
                "action": "APPROVAL_REJECTED",
                "approver": "operator",
                "reason": reason,
            },
        )
        return updated

    # pending_approval_tasks() below complements the richer approve_task /
    # reject_task API (kept from the parallel fix); the duplicate minimal
    # approve_task was removed to avoid shadowing the approver-aware version.
    def pending_approval_tasks(self) -> list[Task]:
        """List tasks waiting for explicit operator approval.

        Covers both queue states the gate can leave a task in: READY (freshly
        stamped, not yet inspected by the swarm) and BLOCKED (flipped by the
        swarm's BLOCKED_ON_APPROVAL defense-in-depth check).
        """
        pending: list[Task] = []
        for status in (TaskStatus.READY, TaskStatus.BLOCKED):
            for t in self._task_manager.list_tasks(status=status):
                if t.requires_approval:
                    pending.append(t)
        return pending

    def execute_next(self) -> Any:
        """Trigger swarm execution on the next queued batch."""
        return self._swarm.run_queue()

    def execute_task_now(self, task: Task) -> Any:
        """Execute one task synchronously, bypassing the queue batch."""
        return self._swarm.execute_task(task)

    def submit_job(
        self,
        task: str,
        provider: str,
        account: str | None = None,
        worker: str | None = None,
        model: str | None = None,
        failover_chain: list[dict[str, Any]] | None = None,
    ) -> Job:
        """Submit a deterministic Job to any AIProvider or AgentAdapter."""
        return self._job_manager.submit_job(
            task=task,
            provider=provider,
            account=account,
            worker=worker,
            model=model,
            failover_chain=failover_chain,
        )

    def execute_job(
        self, job: Job, failover_chain: list[dict[str, Any]] | None = None
    ) -> Job:
        """Execute a Job deterministically with explicit failovers."""
        return self._job_manager.execute_job(job=job, failover_chain=failover_chain)

    # ------------------------------------------------------------------
    # Continue
    # ------------------------------------------------------------------
    def build_continue_context(self) -> ContinueContext:
        return UniversalContinuator(
            task_manager=self._task_manager,
            handoff_manager=self._handoff_manager,
            workspace_dir=self._workspace,
            session_manager=self._session_manager,
            memory_store=self._memory_store,
        ).build_continue_context()

    def continue_work(self) -> tuple[ContinueContext, Any]:
        """Resume where work stopped and execute the next step."""
        ctx = self.build_continue_context()
        if ctx.is_terminal:
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                metadata={
                    "reason": ctx.terminal_reason,
                    "task_id": ctx.active_task.task_id if ctx.active_task else None,
                },
            )
            return ctx, None

        self._event_bus.publish(
            EventType.CONTINUATION_STARTED,
            agent_id=ctx.target_agent,
            task_id=ctx.active_task.task_id if ctx.active_task else None,
            metadata={
                "task_type": ctx.task_type,
                "continuation_depth": ctx.continuation_depth,
                "verification_depth": ctx.verification_depth,
                "continuation_budget": ctx.continuation_budget,
            },
        )

        task = self.plan_and_dispatch(
            instruction=ctx.next_recommended_action,
            preferred_agent=ctx.target_agent,
            preferred_model=None if ctx.target_model == "auto" else ctx.target_model,
            conversation_id=ctx.resume_conversation_id,
            task_type=ctx.task_type,
            parent_task=ctx.active_task.task_id if ctx.active_task else None,
            continuation_depth=ctx.continuation_depth,
            max_continuation_depth=ctx.max_continuation_depth,
            verification_depth=ctx.verification_depth,
            max_verification_depth=ctx.max_verification_depth,
            continuation_budget=ctx.continuation_budget,
            verification_for=ctx.active_task.task_id if (ctx.task_type == "verification" and ctx.active_task) else None,
        )
        if task.is_terminal_state:
            return ctx, None
        result = self._swarm.execute_task(task)
        return ctx, result

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def health(self, agent_id: str | None = None, deep: bool = False) -> dict[str, Any]:
        """Health for one agent or all of them.

        `deep=True` performs a provider round-trip where the adapter supports
        one. It is never run implicitly, to avoid spending model quota.
        """
        adapters = (
            [self._registry.get_adapter(agent_id)]
            if agent_id
            else [a for p in self._registry.list_providers() for a in p.adapters.values()]
        )
        report: dict[str, Any] = {}
        for adapter in adapters:
            if adapter is None:
                continue
            try:
                healthy, reason = adapter.health(deep=deep)  # type: ignore[call-arg]
            except TypeError:
                healthy, reason = adapter.health()
            entry = adapter.describe()
            entry["health"] = {"healthy": healthy, "reason": reason, "deep": deep}
            session = self._session_manager.get_latest_for_agent(adapter.agent_id)
            entry["last_session"] = session.to_dict() if session else None
            report[adapter.agent_id] = entry

            self._event_bus.publish(
                EventType.AGENT_HEALTH,
                agent_id=adapter.agent_id,
                metadata={
                    "healthy": healthy,
                    "reason": reason,
                    "deep": deep,
                    "account_id": adapter.account_id,
                },
            )
            if adapter.agent_id == "antigravity-account-2":
                self._event_bus.publish(
                    EventType.ANTIGRAVITY_ACCOUNT2_HEALTH,
                    agent_id=adapter.agent_id,
                    metadata={"healthy": healthy, "reason": reason, "deep": deep},
                )
        return report
