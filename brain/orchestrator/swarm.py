"""Multi-Agent Swarm Worker and Execution Pool.

Coordinates concurrent headless task execution across the four active agents:

- Antigravity Account 1 (profile `antigravity-cli`)
- Antigravity Account 2 (profile `antigravity-ide`) — headless, no IDE required
- Kiro CLI
- Cline CLI

Execution path for every task:

    brain -> router -> account adapter -> CLI -> JSON -> normalized result
          -> task record -> session mapping -> shared memory -> handoff -> events

Enforces bounded concurrency (MAX_CONCURRENT_AGENTS, default 2 for a 2-core /
16 GB host), file locking, least-privilege permissions, honest model reporting,
task-to-conversation session mapping, and per-account telemetry.
"""
from __future__ import annotations

import concurrent.futures
import datetime
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from agents.base.adapter import UNKNOWN_MODEL, AgentAdapter, TaskExecutionResult
from brain.context.context_optimizer import ContextOptimizer, TokenTelemetryTracker
from brain.orchestrator.failover import FailoverManager
from brain.worktree.worktree_manager import WorktreeManager, WorktreeRecord, WorktreeStatus
from events.bus import EventBus, EventType
from handoffs.handoff_manager import HandoffManager, HandoffRecord
from locks.file_locker import FileLocker
from memory.retrieval.retriever import MemoryRetriever
from memory.store.memory_store import MemoryScope, MemoryStore
from models.policies.model_policy import verify_actual_model
from providers.registry.bootstrap import create_default_registry
from providers.registry.provider_registry import ProviderRegistry
from sessions.session_manager import SessionManager
from tasks.manager import Task, TaskManager, TaskStatus

MAX_CONCURRENT_AGENTS = int(os.environ.get("MAX_CONCURRENT_AGENTS", "2"))
MAX_HEAVY_AGENTS = int(os.environ.get("MAX_HEAVY_AGENTS", "1"))

#: Per-account telemetry event names. Account 2 has dedicated events as required
#: by the observability contract; the mapping keeps the swarm free of
#: agent-specific branching.
ACCOUNT_EVENTS: dict[str, dict[str, EventType]] = {
    "antigravity-account-2": {
        "started": EventType.ANTIGRAVITY_ACCOUNT2_STARTED,
        "completed": EventType.ANTIGRAVITY_ACCOUNT2_COMPLETED,
        "failed": EventType.ANTIGRAVITY_ACCOUNT2_FAILED,
        "health": EventType.ANTIGRAVITY_ACCOUNT2_HEALTH,
        "model_selected": EventType.ANTIGRAVITY_ACCOUNT2_MODEL_SELECTED,
        "handoff": EventType.ANTIGRAVITY_ACCOUNT2_HANDOFF,
    },
}

#: Which agent should verify another agent's output, so a completed task hands
#: off to a genuinely different agent rather than looping on itself.
VERIFICATION_AGENT = "kiro-cli"


class SwarmWorkerPool:
    """Manages concurrent headless execution of tasks using registered adapters."""

    def __init__(
        self,
        task_manager: TaskManager,
        registry: ProviderRegistry | None = None,
        event_bus: EventBus | None = None,
        file_locker: FileLocker | None = None,
        handoff_manager: HandoffManager | None = None,
        memory_store: MemoryStore | None = None,
        session_manager: SessionManager | None = None,
        workspace_dir: Path | None = None,
        max_workers: int = MAX_CONCURRENT_AGENTS,
        worktree_manager: WorktreeManager | None = None,
        context_optimizer: ContextOptimizer | None = None,
        token_tracker: TokenTelemetryTracker | None = None,
        failover_manager: FailoverManager | None = None,
    ) -> None:
        self._task_manager = task_manager
        self._registry = registry or create_default_registry()
        self._event_bus = event_bus or EventBus()
        self._file_locker = file_locker or FileLocker()
        self._handoff_manager = handoff_manager or HandoffManager()
        self._memory_store = memory_store or MemoryStore()
        self._retriever = MemoryRetriever(self._memory_store, event_bus=self._event_bus)
        self._session_manager = session_manager or SessionManager()
        self._workspace = workspace_dir or Path.cwd()
        self._max_workers = max(1, max_workers)
        self._worktree_manager = worktree_manager or WorktreeManager(canonical_repo=self._workspace)
        self._context_optimizer = context_optimizer or ContextOptimizer(event_bus=self._event_bus)
        self._token_tracker = token_tracker or TokenTelemetryTracker(event_bus=self._event_bus)
        self._failover_manager = failover_manager or FailoverManager(registry=self._registry, event_bus=self._event_bus)
        self._max_heavy_agents = max(1, MAX_HEAVY_AGENTS)
        self._active_heavy_tasks = 0
        self._heavy_lock = threading.Lock()
        self._active_tasks: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.Lock()

    @property
    def token_tracker(self) -> TokenTelemetryTracker:
        return self._token_tracker

    @property
    def context_optimizer(self) -> ContextOptimizer:
        return self._context_optimizer

    @property
    def failover_manager(self) -> FailoverManager:
        return self._failover_manager

    @property
    def worktrees(self) -> WorktreeManager:
        return self._worktree_manager

    def get_active_task_ids(self) -> list[str]:
        """Return task IDs of currently executing tasks."""
        with self._active_lock:
            return list(self._active_tasks.keys())

    def get_active_tasks(self) -> dict[str, dict[str, Any]]:
        """Return copy of metadata dict for all currently executing tasks."""
        with self._active_lock:
            return {k: dict(v) for k, v in self._active_tasks.items()}

    def is_task_active(self, task_id: str) -> bool:
        """Check whether a given task is currently executing."""
        with self._active_lock:
            return task_id in self._active_tasks

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------
    def _emit(
        self,
        kind: str,
        generic: EventType,
        adapter: AgentAdapter | None,
        agent_id: str,
        task_id: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Publish the generic event plus any account-specific twin."""
        meta = dict(metadata or {})
        meta.setdefault("account_id", adapter.account_id if adapter else "unknown")
        meta.setdefault("provider", adapter.provider if adapter else "unknown")

        self._event_bus.publish(
            generic,
            agent_id=agent_id,
            task_id=task_id,
            session_id=session_id,
            metadata=meta,
        )
        specific = ACCOUNT_EVENTS.get(agent_id, {}).get(kind)
        if specific:
            self._event_bus.publish(
                specific,
                agent_id=agent_id,
                task_id=task_id,
                session_id=session_id,
                metadata=meta,
            )

    def _git_state(self) -> str:
        """Compact, single-line git summary. Never a full status dump."""
        try:
            status = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True, timeout=10, cwd=str(self._workspace),
            )
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=10, cwd=str(self._workspace),
            )
        except Exception:
            return "unknown"
        if status.returncode != 0:
            return "unknown"
        changed = [line for line in status.stdout.splitlines() if line.strip()]
        branch_name = branch.stdout.strip() or "unknown-branch"
        if not changed:
            return f"clean on {branch_name}"
        return f"{len(changed)} uncommitted change(s) on {branch_name}"

    # ------------------------------------------------------------------
    # Prompt assembly
    # ------------------------------------------------------------------
    def _build_prompt(self, task: Task) -> tuple[str, list[str], dict[str, Any]]:
        """Compose the agent prompt with scoped target files and strict token budgets."""
        memories = self._retriever.retrieve_context(
            query=f"{task.title} {task.description}",
            task_id=task.task_id,
            max_items=5,
            max_bytes=1500,
        )
        memory_refs = [m.memory_id for m in memories]
        context_block = self._retriever.format_context_for_prompt(memories)
        compressed_handoff = self._handoff_manager.get_compressed_handoff(max_chars=2000)

        optimized_prompt, report = self._context_optimizer.optimize_prompt(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            target_files=task.files,
            memory_block=context_block,
            handoff_block=compressed_handoff,
            complexity=task.complexity,
            workspace=self._workspace,
        )

        return optimized_prompt, memory_refs, report.options_applied

    def _resolve_execution_options(self, task: Task, session_id: str) -> dict[str, Any]:
        """Least-privilege options.

        Nothing here silently enables `--dangerously-skip-permissions`. It is
        only set when a task explicitly carries it, or when the account is
        configured for it in config/providers.json.
        """
        options: dict[str, Any] = {"session_id": session_id}
        options.update(task.execution_options or {})
        if task.conversation_id:
            options.setdefault("conversation_id", task.conversation_id)
        return options

    def _is_mutating_task(self, task: Task, options: dict[str, Any]) -> bool:
        """Classify task as READ_ONLY or MUTATING according to Phase 3 policy.

        Tasks that escalate tool permissions, declare target files, request sandbox,
        or invoke mutating actions MUST run inside an isolated git worktree.
        """
        # 1. Explicit execution options overrides
        if "sandbox" in options:
            return bool(options["sandbox"])
        if "is_mutating" in options:
            return bool(options["is_mutating"])
        if getattr(task, "execution_options", None):
            if "sandbox" in task.execution_options:
                return bool(task.execution_options["sandbox"])
            if "is_mutating" in task.execution_options:
                return bool(task.execution_options["is_mutating"])

        # 2. Tool permissions escalation automatically triggers worktree isolation
        if (
            options.get("dangerously_skip_permissions")
            or options.get("allow_tool_permissions")
            or options.get("trust_all_tools")
            or options.get("auto_approve")
        ):
            return True

        # 3. File targets declared
        if task.files:
            return True

        # 4. Tools requested
        if task.tools:
            return True

        # 5. Semantic keyword analysis of task text
        text = f"{task.title} {task.description}".lower()
        mutating_verbs = {
            "create", "write", "modify", "delete", "edit", "refactor",
            "update", "fix", "add", "patch", "implement", "build",
            "scaffold", "generate", "remove", "touch", "rename"
        }
        read_only_phrases = {
            "inspect", "explain", "analyze", "review logs", "show",
            "describe", "what is", "how do", "architecture review", "list"
        }
        words = set(re.findall(r"\b[a-z-]+\b", text))
        has_mutating = bool(words & mutating_verbs)
        has_read_only = any(phrase in text for phrase in read_only_phrases)

        if has_mutating:
            return True
        if has_read_only:
            return False

        return False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute_task(
        self,
        task: Task,
        attempted_agents: list[str] | None = None,
    ) -> TaskExecutionResult:
        """Execute a single task synchronously on its assigned agent."""
        agent_id = task.assigned_agent or "antigravity-account-1"

        # BUG-002 fix (defense in depth): a task flagged for operator approval
        # must never be executed here, no matter which caller reaches this
        # entry point (queue batch, dashboard, continuation, or direct call).
        if getattr(task, "requires_approval", False):
            self._task_manager.update_status(
                task.task_id,
                TaskStatus.BLOCKED,
                stage="BLOCKED_ON_APPROVAL",
                error=task.approval_reason
                or "Task requires explicit operator approval before execution",
            )
            self._event_bus.publish(
                EventType.APPROVAL_REQUIRED,
                metadata={"blocked_tasks": [task.task_id], "source": "swarm.execute_task"},
            )
            return TaskExecutionResult(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id=task.assigned_account or "unknown",
                provider="approval-gate",
                success=False,
                exit_code=1,
                output="",
                error="BLOCKED_ON_APPROVAL: "
                + (task.approval_reason or "explicit operator approval required"),
                actual_model=UNKNOWN_MODEL,
            )

        # Pre-flight Phase 4A guards: check terminal states and depth/budget limits
        if task.is_terminal_state:
            return TaskExecutionResult(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id="unknown",
                provider="unknown",
                success=False,
                exit_code=1,
                output="",
                error=f"Task {task.task_id} is already in a terminal state ({task.status.value})",
                actual_model=UNKNOWN_MODEL,
            )

        if task.continuation_budget <= 0:
            self._task_manager.update_status(
                task.task_id,
                TaskStatus.BUDGET_EXHAUSTED,
                error="Continuation budget exhausted",
                is_terminal=True,
                terminal_reason="BUDGET_EXHAUSTED",
            )
            self._event_bus.publish(
                EventType.BUDGET_EXHAUSTED,
                agent_id=agent_id,
                task_id=task.task_id,
                metadata={"budget": task.continuation_budget},
            )
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                agent_id=agent_id,
                task_id=task.task_id,
                metadata={"reason": "BUDGET_EXHAUSTED"},
            )
            return TaskExecutionResult(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id="unknown",
                provider="unknown",
                success=False,
                exit_code=1,
                output="",
                error="Continuation budget exhausted",
                actual_model=UNKNOWN_MODEL,
            )

        if task.continuation_depth > task.max_continuation_depth:
            self._task_manager.update_status(
                task.task_id,
                TaskStatus.DEPTH_LIMIT_REACHED,
                error=f"Continuation depth limit ({task.max_continuation_depth}) reached",
                is_terminal=True,
                terminal_reason="DEPTH_LIMIT_REACHED",
            )
            self._event_bus.publish(
                EventType.DEPTH_LIMIT_REACHED,
                agent_id=agent_id,
                task_id=task.task_id,
                metadata={"depth": task.continuation_depth, "max_depth": task.max_continuation_depth},
            )
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                agent_id=agent_id,
                task_id=task.task_id,
                metadata={"reason": "DEPTH_LIMIT_REACHED"},
            )
            return TaskExecutionResult(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id="unknown",
                provider="unknown",
                success=False,
                exit_code=1,
                output="",
                error=f"Continuation depth limit ({task.max_continuation_depth}) reached",
                actual_model=UNKNOWN_MODEL,
            )

        if task.is_verification:
            if task.verification_depth > task.max_verification_depth:
                self._task_manager.update_status(
                    task.task_id,
                    TaskStatus.DEPTH_LIMIT_REACHED,
                    error=f"Verification depth limit ({task.max_verification_depth}) exceeded",
                    is_terminal=True,
                    terminal_reason="DEPTH_LIMIT_REACHED",
                )
                self._event_bus.publish(
                    EventType.RECURSION_PREVENTED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    metadata={"verification_depth": task.verification_depth},
                )
                self._event_bus.publish(
                    EventType.DEPTH_LIMIT_REACHED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    metadata={"depth": task.verification_depth, "max_depth": task.max_verification_depth},
                )
                self._event_bus.publish(
                    EventType.CONTINUATION_TERMINATED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    metadata={"reason": "DEPTH_LIMIT_REACHED"},
                )
                return TaskExecutionResult(
                    task_id=task.task_id,
                    agent_id=agent_id,
                    account_id="unknown",
                    provider="unknown",
                    success=False,
                    exit_code=1,
                    output="",
                    error=f"Verification depth limit ({task.max_verification_depth}) exceeded",
                    actual_model=UNKNOWN_MODEL,
                )

            if task.parent_task:
                parent = self._task_manager.get_task(task.parent_task)
                if parent and (parent.is_verification or parent.task_type == "verification"):
                    self._task_manager.update_status(
                        task.task_id,
                        TaskStatus.REJECTED,
                        error="Recursive verification is prohibited",
                        is_terminal=True,
                        terminal_reason="RECURSION_PREVENTED",
                    )
                    self._event_bus.publish(
                        EventType.RECURSION_PREVENTED,
                        agent_id=agent_id,
                        task_id=task.task_id,
                        metadata={"parent_task": task.parent_task},
                    )
                    self._event_bus.publish(
                        EventType.VERIFICATION_REJECTED,
                        agent_id=agent_id,
                        task_id=task.task_id,
                        metadata={"reason": "Recursive verification is prohibited"},
                    )
                    return TaskExecutionResult(
                        task_id=task.task_id,
                        agent_id=agent_id,
                        account_id="unknown",
                        provider="unknown",
                        success=False,
                        exit_code=1,
                        output="",
                        error="Recursive verification is prohibited",
                        actual_model=UNKNOWN_MODEL,
                    )

            self._event_bus.publish(
                EventType.VERIFICATION_STARTED,
                agent_id=agent_id,
                task_id=task.task_id,
                metadata={"verification_for": task.verification_for, "depth": task.verification_depth},
            )

        adapter = self._registry.get_adapter(agent_id)

        if not adapter:
            active = self._registry.list_active_adapters()
            if not active:
                error = "No active agent adapters available"
                self._task_manager.update_status(task.task_id, TaskStatus.FAILED, error=error)
                return TaskExecutionResult(
                    task_id=task.task_id,
                    agent_id=agent_id,
                    account_id="unknown",
                    provider="unknown",
                    success=False,
                    exit_code=1,
                    output="",
                    error=error,
                    actual_model=UNKNOWN_MODEL,
                )
            adapter = active[0]
            agent_id = adapter.agent_id

        session_id = task.session_id or f"sess-{uuid.uuid4().hex[:8]}"

        # Pre-flight health so a dead account fails fast instead of burning a run.
        healthy, health_reason = adapter.health()
        self._emit(
            "health",
            EventType.AGENT_HEALTH,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {"healthy": healthy, "reason": health_reason},
        )
        if not healthy:
            self._task_manager.update_status(
                task.task_id, TaskStatus.BLOCKED, error=f"Agent unhealthy: {health_reason}"
            )
            self._emit(
                "failed",
                EventType.TASK_FAILED,
                adapter,
                agent_id,
                task.task_id,
                session_id,
                {"error": health_reason},
            )
            return TaskExecutionResult(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id=adapter.account_id,
                provider=adapter.provider,
                success=False,
                exit_code=1,
                output="",
                error=f"Agent unhealthy: {health_reason}",
                actual_model=UNKNOWN_MODEL,
                session_id=session_id,
            )

        with self._active_lock:
            self._active_tasks[task.task_id] = {
                "task_id": task.task_id,
                "agent_id": agent_id,
                "provider": adapter.provider,
                "model": task.assigned_model,
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }

        self._task_manager.update_status(
            task.task_id,
            TaskStatus.RUNNING,
            session_id=session_id,
            requested_model=task.assigned_model,
            stage="MEMORY",
        )

        # Record the session mapping *before* execution so an interrupted run
        # still leaves a resumable trace.
        self._session_manager.record_session(
            task_id=task.task_id,
            agent_id=agent_id,
            session_id=session_id,
            model=task.assigned_model,
            metadata={"account_id": adapter.account_id, "provider": adapter.provider},
        )

        self._emit(
            "started",
            EventType.TASK_STARTED,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {"requested_model": task.assigned_model},
        )
        self._emit(
            "model_selected",
            EventType.MODEL_SELECTED,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {"requested_model": task.assigned_model},
        )
        self._event_bus.publish(
            EventType.PROVIDER_STARTED,
            agent_id=agent_id,
            task_id=task.task_id,
            session_id=session_id,
            provider=adapter.provider,
            metadata={
                "requested_model": task.assigned_model,
                "account_id": adapter.account_id,
            },
        )

        # Acquire file locks
        locked_files: list[str] = []
        for file_path in task.files:
            if self._file_locker.acquire(file_path, agent_id=agent_id, task_id=task.task_id):
                locked_files.append(file_path)
                self._event_bus.publish(
                    EventType.FILE_LOCKED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    metadata={"file": file_path},
                )
            else:
                error = f"Failed to acquire lock for file: {file_path}"
                self._task_manager.update_status(task.task_id, TaskStatus.BLOCKED, error=error)
                self._emit(
                    "failed",
                    EventType.TASK_FAILED,
                    adapter,
                    agent_id,
                    task.task_id,
                    session_id,
                    {"error": error},
                )
                for lf in locked_files:
                    self._file_locker.release(lf, agent_id=agent_id)
                # This early return bypasses the try/finally below, so the task
                # must be deregistered here or it stays "active" forever.
                with self._active_lock:
                    self._active_tasks.pop(task.task_id, None)
                return TaskExecutionResult(
                    task_id=task.task_id,
                    agent_id=agent_id,
                    account_id=adapter.account_id,
                    provider=adapter.provider,
                    success=False,
                    exit_code=1,
                    output="",
                    error=error,
                    actual_model=UNKNOWN_MODEL,
                    session_id=session_id,
                )

        is_heavy = task.complexity in ("reasoning", "strong") or agent_id in ("antigravity-account-1", "antigravity-account-2")
        if is_heavy:
            with self._heavy_lock:
                self._active_heavy_tasks += 1

        try:
            prompt, memory_refs, opt_options = self._build_prompt(task)
            options = self._resolve_execution_options(task, session_id)
            options.update(opt_options)

            self._event_bus.publish(
                EventType.TASK_EXECUTION_REQUESTED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"requested_model": task.assigned_model},
            )

            # Check privilege escalation audit
            escalation_requested = bool(
                options.get("dangerously_skip_permissions")
                or options.get("allow_tool_permissions")
                or options.get("trust_all_tools")
                or options.get("auto_approve")
            )
            if escalation_requested:
                self._event_bus.publish(
                    EventType.PERMISSION_ESCALATION_REQUESTED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                )
                self._event_bus.publish(
                    EventType.PERMISSION_ESCALATION_GRANTED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                )

            # Resolve sandbox isolation: mutating or tool-escalated tasks get an isolated worktree
            needs_sandbox = self._is_mutating_task(task, options)
            worktree_record: WorktreeRecord | None = None
            work_dir = self._workspace

            if needs_sandbox and self._worktree_manager.is_git_repository():
                try:
                    worktree_record = self._worktree_manager.create(
                        task_id=task.task_id,
                        agent_id=agent_id,
                        account_id=adapter.account_id,
                    )
                    work_dir = Path(worktree_record.path)
                    self._event_bus.publish(
                        EventType.WORKTREE_CREATED,
                        agent_id=agent_id,
                        task_id=task.task_id,
                        session_id=session_id,
                        metadata={
                            "path": worktree_record.path,
                            "branch": worktree_record.branch,
                            "base_commit": worktree_record.base_commit,
                            "canonical_dirty": worktree_record.canonical_dirty_at_creation,
                            "canonical_dirty_count": worktree_record.canonical_dirty_count,
                        },
                    )
                    prompt += (
                        f"\n\n[SANDBOX WORKSPACE]\n"
                        f"You are executing in an isolated Git worktree at: {worktree_record.path}\n"
                        f"Branch: {worktree_record.branch}\n"
                        f"The canonical project repository is completely protected and untouched.\n"
                        f"Make all requested file modifications directly in this isolated worktree directory.\n"
                        f"Do not commit, push, or switch branches; your modifications will be reviewed and diffed automatically."
                    )
                except Exception as wt_err:
                    error = f"Failed to initialize sandbox worktree: {wt_err}"
                    self._task_manager.update_status(task.task_id, TaskStatus.FAILED, error=error)
                    self._event_bus.publish(
                        EventType.TASK_EXECUTION_FAILED,
                        agent_id=agent_id,
                        task_id=task.task_id,
                        session_id=session_id,
                        metadata={"error": error},
                    )
                    return TaskExecutionResult(
                        task_id=task.task_id,
                        agent_id=agent_id,
                        account_id=adapter.account_id,
                        provider=adapter.provider,
                        success=False,
                        exit_code=1,
                        output="",
                        error=error,
                        actual_model=UNKNOWN_MODEL,
                        session_id=session_id,
                    )

            self._task_manager.update_status(
                task.task_id, TaskStatus.RUNNING, stage="EXECUTION"
            )
            self._event_bus.publish(
                EventType.TASK_EXECUTION_STARTED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"work_dir": str(work_dir), "sandboxed": worktree_record is not None},
            )

            result = adapter.execute(
                task_id=task.task_id,
                prompt=prompt,
                model=task.assigned_model,
                work_dir=work_dir,
                timeout_seconds=300,
                options=options,
            )

            # If executing in sandbox, capture snapshot and diff
            if worktree_record:
                snapshot = self._worktree_manager.snapshot(task.task_id)
                diff_info = self._worktree_manager.diff(task.task_id)
                if snapshot.get("changed"):
                    result.files_touched = list(snapshot.get("files_changed", []))
                    for f in result.files_touched:
                        if f not in task.files:
                            task.files.append(f)
                result.raw_response["worktree"] = worktree_record.to_dict()
                result.raw_response["sandbox_snapshot"] = snapshot
                result.raw_response["diff"] = diff_info

            # Honest model reporting: only what the provider actually named.
            result.actual_model = verify_actual_model(result.raw_response, task.assigned_model)
            result.session_id = session_id

            self._session_manager.record_session(
                task_id=task.task_id,
                agent_id=agent_id,
                session_id=session_id,
                conversation_id=result.conversation_id,
                model=task.assigned_model,
                metadata={
                    "account_id": adapter.account_id,
                    "provider": adapter.provider,
                    "actual_model": result.actual_model,
                    "exit_code": result.exit_code,
                    "sandboxed": worktree_record is not None,
                },
            )

            # Record token usage honestly via TokenTelemetryTracker
            self._token_tracker.record_usage(
                task_id=task.task_id,
                agent_id=agent_id,
                account_id=adapter.account_id,
                provider=adapter.provider,
                requested_model=task.assigned_model,
                actual_model=result.actual_model,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                total_tokens=result.total_tokens,
                cache_read_tokens=result.cache_read_tokens,
                duration_seconds=result.duration_seconds,
                task_type=task.task_type,
                complexity=task.complexity,
                success=result.success,
                usage_source=result.usage_source,
                started_at=result.started_at,
                completed_at=result.completed_at,
                raw_response=result.raw_response,
            )

            if result.success:
                self._event_bus.publish(
                    EventType.PROVIDER_COMPLETED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                    provider=adapter.provider,
                    metadata={
                        "actual_model": result.actual_model,
                        "exit_code": result.exit_code,
                        "duration_seconds": result.duration_seconds,
                        "total_tokens": result.total_tokens,
                        "usage_source": result.usage_source,
                    },
                )
                self._on_success(task, adapter, agent_id, session_id, result, memory_refs, worktree_record)
            else:
                self._event_bus.publish(
                    EventType.PROVIDER_FAILED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                    provider=adapter.provider,
                    metadata={
                        "exit_code": result.exit_code,
                        "error": (result.error or "")[:500],
                        "provider_status": result.provider_status,
                    },
                )
                self._emit(
                    "failed",
                    EventType.TASK_EXECUTION_FAILED,
                    adapter,
                    agent_id,
                    task.task_id,
                    session_id,
                    {
                        "error": (result.error or "")[:500],
                        "exit_code": result.exit_code,
                        "provider_status": result.provider_status,
                    },
                )
                # Check for failover opportunity (e.g. Rate limit, quota, timeout, CLI error)
                attempted = list(attempted_agents or [agent_id])
                failover_dec = self._failover_manager.evaluate_failover(
                    task=task,
                    failed_agent_id=agent_id,
                    error_text=result.error or "",
                    exit_code=result.exit_code,
                    attempted_agents=attempted,
                )
                if failover_dec.should_failover and failover_dec.fallback_agent_id:
                    fallback_agent = failover_dec.fallback_agent_id
                    fb_adapter = self._registry.get_adapter(fallback_agent)
                    fallback_record = {
                        "original_provider": adapter.provider,
                        "original_agent": agent_id,
                        "original_model": task.assigned_model,
                        "failure": (result.error or "")[:500],
                        "fallback_agent": fallback_agent,
                        "fallback_provider": getattr(fb_adapter, "provider", "unknown") if fb_adapter else "unknown",
                        "fallback_model": getattr(fb_adapter, "default_model", "auto") if fb_adapter else "auto",
                        "fallback_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "reason": failover_dec.reason,
                    }
                    task.result["fallback"] = fallback_record
                    self._task_manager.update_status(
                        task.task_id, TaskStatus.RUNNING, stage="FAILOVER"
                    )
                    self._event_bus.publish(
                        EventType.FAILOVER_STARTED,
                        agent_id=fallback_agent,
                        task_id=task.task_id,
                        session_id=session_id,
                        provider=fb_adapter.provider if fb_adapter else "unknown",
                        metadata=fallback_record,
                    )
                    if worktree_record and not result.files_touched:
                        try:
                            self._worktree_manager.cleanup(task.task_id)
                        except Exception:
                            pass
                    task.assigned_agent = fallback_agent
                    if fb_adapter:
                        task.assigned_account = fb_adapter.account_id
                    for lf in locked_files:
                        self._file_locker.release(lf, agent_id=agent_id)
                    locked_files.clear()
                    fallback_res = self.execute_task(task, attempted_agents=attempted + [fallback_agent])
                    self._event_bus.publish(
                        EventType.FAILOVER_COMPLETED,
                        agent_id=fallback_agent,
                        task_id=task.task_id,
                        session_id=session_id,
                        provider=fb_adapter.provider if fb_adapter else "unknown",
                        metadata={
                            "success": fallback_res.success,
                            "fallback_agent": fallback_agent,
                        },
                    )
                    return fallback_res

                self._on_failure(task, adapter, agent_id, session_id, result, worktree_record)

            return result
        finally:
            with self._active_lock:
                self._active_tasks.pop(task.task_id, None)
            if is_heavy:
                with self._heavy_lock:
                    self._active_heavy_tasks = max(0, self._active_heavy_tasks - 1)
            for lf in locked_files:
                self._file_locker.release(lf, agent_id=agent_id)
                self._event_bus.publish(
                    EventType.FILE_UNLOCKED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    metadata={"file": lf},
                )

    def _on_success(
        self,
        task: Task,
        adapter: AgentAdapter,
        agent_id: str,
        session_id: str,
        result: TaskExecutionResult,
        memory_refs: list[str],
        worktree_record: WorktreeRecord | None = None,
    ) -> None:
        # Shared memory: one compact, useful entry. Never the raw payload.
        stored_refs = list(memory_refs)
        if result.output:
            snippet = " ".join(result.output.split())[:500]
            entry = self._memory_store.add(
                content=(
                    f"Task '{task.title}' completed by {agent_id} "
                    f"(account {adapter.account_id}): {snippet}"
                ),
                scope=MemoryScope.PROJECT,
                source_agent=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                importance=3,
                tags=["task_result", agent_id, adapter.account_id],
            )
            stored_refs.append(entry.memory_id)
            self._event_bus.publish(
                EventType.MEMORY_CREATED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"memory_id": entry.memory_id, "scope": MemoryScope.PROJECT.value},
            )

        git_state = (
            f"Isolated sandbox on branch {worktree_record.branch} ({len(result.files_touched)} files modified)"
            if (worktree_record and result.files_touched)
            else self._git_state()
        )

        if task.is_verification:
            # Phase 4A: Terminal verification contract
            # Verification tasks MUST terminate the verification loop!
            final_status = TaskStatus.VERIFICATION_COMPLETE
            is_terminal = True
            terminal_reason = "VERIFICATION_COMPLETE"
            remaining_work: list[str] = []
            next_action = f"Verification of task {task.verification_for or task.task_id} completed successfully; workflow is complete."
            recommended_agent = "none"
            recommended_model = "none"

            self._event_bus.publish(
                EventType.VERIFICATION_COMPLETED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={
                    "verification_for": task.verification_for,
                    "depth": task.verification_depth,
                },
            )
            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"reason": "VERIFICATION_COMPLETE"},
            )
        else:
            # Normal task completed. Check if verification is eligible
            can_verify = (
                task.verification_depth < task.max_verification_depth
                and task.continuation_budget > 0
                and task.continuation_depth < task.max_continuation_depth
            )
            existing_verif = [
                t for t in self._task_manager.list_tasks()
                if t.verification_for == task.task_id
                or (t.task_type == "verification" and task.task_id in t.description)
            ]
            if can_verify and not existing_verif:
                recommended_agent = (
                    VERIFICATION_AGENT if agent_id != VERIFICATION_AGENT else "antigravity-account-1"
                )
                recommended_model = "auto"
                if worktree_record and result.files_touched:
                    next_action = f"Review diff for task {task.task_id} on branch {worktree_record.branch} and approve/apply changes."
                else:
                    next_action = f"Verify output of task {task.task_id} ('{task.title}') and integrate it."
                if result.conversation_id:
                    next_action += (
                        f" Resume the originating conversation with "
                        f"--conversation={result.conversation_id} if deeper context is required."
                    )
                remaining_work = ["Verify the produced result and integrate it"]
                is_terminal = False
                terminal_reason = None
                final_status = TaskStatus.COMPLETED
            else:
                next_action = f"Task {task.task_id} completed successfully; workflow complete."
                recommended_agent = "none"
                recommended_model = "none"
                remaining_work = []
                is_terminal = True
                terminal_reason = "COMPLETED"
                final_status = TaskStatus.COMPLETED
                self._event_bus.publish(
                    EventType.CONTINUATION_TERMINATED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                    metadata={"reason": "COMPLETED"},
                )

        record = HandoffRecord(
            task=task.title,
            objective=task.description,
            completed=[
                f"Executed headlessly by {agent_id} (account {adapter.account_id}, "
                f"provider {adapter.provider})",
                f"Requested model {task.assigned_model or 'default'}; "
                f"provider-reported model {result.actual_model}",
                f"Response: {' '.join(result.output.split())[:300]}" if result.output else "No response text",
            ],
            files_modified=task.files,
            tests=["python3 -m unittest discover -s tests"],
            errors=task.errors or [],
            decisions=[
                "Least-privilege execution: --dangerously-skip-permissions not used "
                "unless explicitly configured",
                f"Session {session_id} maps to conversation {result.conversation_id}",
            ],
            relevant_memory=stored_refs,
            git_state=git_state,
            remaining_work=remaining_work,
            next_action=next_action,
            recommended_agent=recommended_agent,
            recommended_model=recommended_model,
            task_id=task.task_id,
            agent_id=agent_id,
            account_id=adapter.account_id,
            session_id=session_id,
            conversation_id=result.conversation_id,
            parent_task_id=task.task_id,
            task_type=task.task_type,
            continuation_depth=task.continuation_depth,
            max_continuation_depth=task.max_continuation_depth,
            verification_depth=task.verification_depth,
            max_verification_depth=task.max_verification_depth,
            continuation_budget=task.continuation_budget,
            is_terminal=is_terminal,
            terminal_reason=terminal_reason,
            verification_for=task.verification_for,
        )
        self._task_manager.update_status(task.task_id, TaskStatus.RUNNING, stage="HANDOFF")
        handoff_path = self._handoff_manager.write_handoff(record)
        self._event_bus.publish(
            EventType.HANDOFF_CREATED,
            agent_id=agent_id,
            task_id=task.task_id,
            session_id=session_id,
            metadata={"recommended_agent": recommended_agent, "handoff": str(handoff_path)},
        )

        stage = "COMPLETE" if final_status in (TaskStatus.COMPLETED, TaskStatus.VERIFICATION_COMPLETE) else "FAILED"
        self._task_manager.update_status(
            task.task_id,
            final_status,
            stage=stage,
            result=result.normalized(),
            actual_model=result.actual_model,
            requested_model=task.assigned_model,
            session_id=session_id,
            conversation_id=result.conversation_id,
            duration_seconds=result.duration_seconds,
            handoff=str(handoff_path),
            memory_refs=stored_refs,
            is_terminal=is_terminal,
            terminal_reason=terminal_reason,
        )

        self._event_bus.publish(
            EventType.TASK_EXECUTION_COMPLETED,
            agent_id=agent_id,
            task_id=task.task_id,
            session_id=session_id,
            metadata={
                "exit_code": result.exit_code,
                "files_touched": result.files_touched,
                "sandboxed": worktree_record is not None,
            },
        )

        self._emit(
            "completed",
            EventType.TASK_COMPLETED,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {
                "requested_model": task.assigned_model,
                "actual_model": result.actual_model,
                "duration_seconds": result.duration_seconds,
                "conversation_id": result.conversation_id,
                "total_tokens": result.total_tokens,
            },
        )
        self._emit(
            "handoff",
            EventType.TASK_HANDOFF,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {"handoff": str(handoff_path), "recommended_agent": recommended_agent},
        )

    def _on_failure(
        self,
        task: Task,
        adapter: AgentAdapter,
        agent_id: str,
        session_id: str,
        result: TaskExecutionResult,
        worktree_record: WorktreeRecord | None = None,
    ) -> None:
        if worktree_record:
            worktree_record.status = WorktreeStatus.FAILED
            worktree_record.error = result.error or f"exit {result.exit_code}"
            self._worktree_manager._save_registry()

        allow_remediation = True
        if task.execution_options and "allow_remediation" in task.execution_options:
            allow_remediation = bool(task.execution_options["allow_remediation"])

        can_remediate = (
            allow_remediation
            and task.continuation_budget > 1
            and task.continuation_depth < task.max_continuation_depth
        )

        if task.is_verification:
            self._event_bus.publish(
                EventType.VERIFICATION_REJECTED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"error": (result.error or "")[:500]},
            )

        if can_remediate:
            # Bounded remediation permitted
            next_action = f"Remediate failure in task {task.task_id}: {(result.error or '')[:200]}"
            recommended_agent = task.assigned_agent or "antigravity-account-1"
            is_terminal = False
            terminal_reason = None
            final_status = TaskStatus.FAILED

            record = HandoffRecord(
                task=task.title,
                objective=task.description,
                completed=[],
                files_modified=task.files,
                tests=["python3 -m unittest discover -s tests"],
                errors=[result.error or f"exit {result.exit_code}"],
                decisions=[],
                git_state=self._git_state(),
                remaining_work=[f"Remediate failure: {(result.error or '')[:200]}"],
                next_action=next_action,
                recommended_agent=recommended_agent,
                recommended_model="auto",
                task_id=task.task_id,
                agent_id=agent_id,
                account_id=adapter.account_id,
                session_id=session_id,
                conversation_id=result.conversation_id,
                parent_task_id=task.task_id,
                task_type="remediation",
                continuation_depth=task.continuation_depth + 1,
                max_continuation_depth=task.max_continuation_depth,
                verification_depth=task.verification_depth,
                max_verification_depth=task.max_verification_depth,
                continuation_budget=task.continuation_budget - 1,
                is_terminal=False,
                terminal_reason=None,
                verification_for=task.verification_for,
            )
            handoff_path = self._handoff_manager.write_handoff(record)
        else:
            # Terminal failure: budget exhausted or depth limit reached
            is_terminal = True
            if task.continuation_budget <= 1:
                final_status = TaskStatus.BUDGET_EXHAUSTED
                terminal_reason = "BUDGET_EXHAUSTED"
                self._event_bus.publish(
                    EventType.BUDGET_EXHAUSTED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                    metadata={"budget": task.continuation_budget},
                )
            elif task.continuation_depth >= task.max_continuation_depth:
                final_status = TaskStatus.DEPTH_LIMIT_REACHED
                terminal_reason = "DEPTH_LIMIT_REACHED"
                self._event_bus.publish(
                    EventType.DEPTH_LIMIT_REACHED,
                    agent_id=agent_id,
                    task_id=task.task_id,
                    session_id=session_id,
                    metadata={"depth": task.continuation_depth},
                )
            else:
                final_status = TaskStatus.FAILED
                terminal_reason = "FAILED"

            self._event_bus.publish(
                EventType.CONTINUATION_TERMINATED,
                agent_id=agent_id,
                task_id=task.task_id,
                session_id=session_id,
                metadata={"reason": terminal_reason},
            )

            record = HandoffRecord(
                task=task.title,
                objective=task.description,
                completed=[],
                files_modified=task.files,
                tests=["python3 -m unittest discover -s tests"],
                errors=[result.error or f"exit {result.exit_code}"],
                decisions=[],
                git_state=self._git_state(),
                remaining_work=[],
                next_action=f"Continuation terminated: {terminal_reason}",
                recommended_agent="none",
                recommended_model="none",
                task_id=task.task_id,
                agent_id=agent_id,
                account_id=adapter.account_id,
                session_id=session_id,
                conversation_id=result.conversation_id,
                parent_task_id=task.task_id,
                task_type=task.task_type,
                continuation_depth=task.continuation_depth,
                max_continuation_depth=task.max_continuation_depth,
                verification_depth=task.verification_depth,
                max_verification_depth=task.max_verification_depth,
                continuation_budget=task.continuation_budget,
                is_terminal=True,
                terminal_reason=terminal_reason,
                verification_for=task.verification_for,
            )
            handoff_path = self._handoff_manager.write_handoff(record)

        self._task_manager.update_status(
            task.task_id,
            final_status,
            result=result.normalized(),
            error=result.error or f"exit {result.exit_code}",
            actual_model=result.actual_model,
            requested_model=task.assigned_model,
            session_id=session_id,
            conversation_id=result.conversation_id,
            duration_seconds=result.duration_seconds,
            handoff=str(handoff_path),
            is_terminal=is_terminal,
            terminal_reason=terminal_reason,
        )

        self._event_bus.publish(
            EventType.TASK_EXECUTION_FAILED,
            agent_id=agent_id,
            task_id=task.task_id,
            session_id=session_id,
            metadata={
                "error": (result.error or "")[:500],
                "exit_code": result.exit_code,
                "sandboxed": worktree_record is not None,
            },
        )

        self._emit(
            "failed",
            EventType.TASK_FAILED,
            adapter,
            agent_id,
            task.task_id,
            session_id,
            {
                "error": (result.error or "")[:500],
                "exit_code": result.exit_code,
                "provider_status": result.provider_status,
            },
        )

    def run_queue(self) -> list[TaskExecutionResult]:
        """Execute the next batch of READY tasks with bounded concurrency."""
        ready_tasks = self._task_manager.list_tasks(status=TaskStatus.READY)
        # BUG-002 fix (defense in depth): never auto-execute tasks flagged at
        # dispatch time as requiring explicit operator approval (destructive
        # instructions). The swarm re-checks the flag at pickup, so no queue
        # consumer can bypass the Phase 18 approval gate.
        gated = [t for t in ready_tasks if getattr(t, "requires_approval", False)]
        for t in gated:
            self._task_manager.update_status(
                t.task_id,
                TaskStatus.BLOCKED,
                stage="BLOCKED_ON_APPROVAL",
                error=t.approval_reason
                or "Task requires explicit operator approval before execution",
            )
        if gated:
            self._event_bus.publish(
                EventType.APPROVAL_REQUIRED,
                metadata={
                    "blocked_tasks": [t.task_id for t in gated],
                    "source": "swarm.run_queue",
                },
            )
        ready_tasks = [t for t in ready_tasks if not getattr(t, "requires_approval", False)]
        if not ready_tasks:
            return []

        results: list[TaskExecutionResult] = []
        # Ensure at most 1 heavy task runs concurrently on the 2-core host
        heavy_seen = False
        selected_batch: list[Task] = []
        for t in ready_tasks:
            is_heavy = t.complexity in ("reasoning", "strong") or t.assigned_agent in ("antigravity-account-1", "antigravity-account-2")
            if is_heavy:
                if not heavy_seen:
                    selected_batch.append(t)
                    heavy_seen = True
            else:
                selected_batch.append(t)
            if len(selected_batch) >= self._max_workers:
                break
        batch = selected_batch or ready_tasks[: self._max_workers]

        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            future_to_task = {executor.submit(self.execute_task, t): t for t in batch}
            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    self._task_manager.update_status(
                        task.task_id, TaskStatus.FAILED, error=str(exc)
                    )
        return results
