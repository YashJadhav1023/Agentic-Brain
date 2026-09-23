"""Persistent Multi-Agent Task Lifecycle and Queue Manager.

Persists task records in structured directories so that state survives restarts,
crashes, or shell closes.
"""
from __future__ import annotations

import datetime
import json
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
import os
from pathlib import Path
from typing import Any


def _write_text_atomic(target: Path, content: str) -> None:
    """Write via a unique temp file in the same directory, then os.replace.

    Readers (dashboard polling, other workers) never observe a truncated file,
    and a failed write leaves the previous record intact.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _brain_dir() -> Path:
    """Shared-brain store root, relocatable via BRAIN_DIR (see dashboard.brain_dir)."""
    configured = os.environ.get("BRAIN_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "agentic-brain"



class TaskStatus(str, Enum):
    BACKLOG = "BACKLOG"
    READY = "READY"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    REVIEW = "REVIEW"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    # Phase 4A Terminal States
    VERIFICATION_COMPLETE = "VERIFICATION_COMPLETE"
    DEPTH_LIMIT_REACHED = "DEPTH_LIMIT_REACHED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    REJECTED = "REJECTED"


class TaskPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class Task:
    task_id: str
    title: str
    description: str
    status: TaskStatus = TaskStatus.READY
    priority: TaskPriority = TaskPriority.NORMAL
    complexity: str = "standard"
    assigned_agent: str | None = None
    assigned_account: str | None = None
    assigned_model: str | None = None
    actual_model: str | None = None
    parent_task: str | None = None
    dependencies: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float = 0.0
    files: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    memory_refs: list[str] = field(default_factory=list)
    handoffs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    #: Per-task execution overrides handed to the adapter (e.g. explicit
    #: {"dangerously_skip_permissions": true}). Empty means "use the account's
    #: configured least-privilege defaults".
    execution_options: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    conversation_id: str | None = None
    requested_model: str | None = None
    # Phase 4A Continuation & Verification Contract
    task_type: str = "general"               # "general", "verification", "remediation"
    # Phase 18 approval gate: destructive/high-risk instructions are stamped at
    # dispatch time (BUG-002 fix) and must be explicitly approved before the
    # swarm may execute them.
    requires_approval: bool = False
    approval_reason: str | None = None
    continuation_depth: int = 0
    max_continuation_depth: int = 5
    verification_depth: int = 0
    max_verification_depth: int = 1
    continuation_budget: int = 5
    verification_for: str | None = None      # ID of task this task verifies
    stage: str = "QUEUED"
    is_terminal: bool = False
    terminal_reason: str | None = None

    @property
    def is_verification(self) -> bool:
        return self.task_type == "verification" or bool(self.verification_for)

    @property
    def is_terminal_state(self) -> bool:
        return self.is_terminal or self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.VERIFICATION_COMPLETE,
            TaskStatus.DEPTH_LIMIT_REACHED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.REJECTED,
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["priority"] = self.priority.value
        d["id"] = self.task_id
        d["instruction"] = self.title
        d["prompt"] = self.title
        output_val = self.result.get("response") if isinstance(self.result, dict) else (self.result if isinstance(self.result, str) else "")
        d["output"] = output_val
        if self.started_at and self.status == TaskStatus.RUNNING:
            try:
                t0 = datetime.datetime.fromisoformat(self.started_at)
                t_now = datetime.datetime.now(datetime.timezone.utc)
                d["elapsed_seconds"] = max(0.0, (t_now - t0).total_seconds())
            except Exception:
                d["elapsed_seconds"] = self.duration_seconds
        else:
            d["elapsed_seconds"] = self.duration_seconds
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        if "status" in data:
            data["status"] = TaskStatus(data["status"])
        if "priority" in data:
            data["priority"] = TaskPriority(data["priority"])
        return cls(**data)


class TaskManager:
    """Manages file-backed task persistence across queue, active, completed, failed."""

    def __init__(self, root_tasks_dir: Path | None = None, include_swarm: bool | None = None) -> None:
        self._root = root_tasks_dir or Path(__file__).resolve().parent
        self._include_swarm = (root_tasks_dir is None) if include_swarm is None else include_swarm
        self._dir_queue = self._root / "queue"
        self._dir_active = self._root / "active"
        self._dir_completed = self._root / "completed"
        self._dir_failed = self._root / "failed"

        for d in (self._dir_queue, self._dir_active, self._dir_completed, self._dir_failed):
            d.mkdir(parents=True, exist_ok=True)
        # Serialises read-modify-write in update_status and the move in save_task
        # so concurrent workers in this process cannot lose each other's updates.
        self._lock = threading.RLock()

    def _dir_for_status(self, status: TaskStatus) -> Path:
        if status in (TaskStatus.READY, TaskStatus.BACKLOG):
            return self._dir_queue
        elif status in (TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.REVIEW):
            return self._dir_active
        elif status in (TaskStatus.COMPLETED, TaskStatus.VERIFICATION_COMPLETE):
            return self._dir_completed
        else:
            return self._dir_failed

    def create_task(
        self,
        title: str,
        description: str,
        priority: TaskPriority = TaskPriority.NORMAL,
        complexity: str = "standard",
        assigned_agent: str | None = None,
        assigned_account: str | None = None,
        assigned_model: str | None = None,
        dependencies: list[str] | None = None,
        parent_task: str | None = None,
        task_id: str | None = None,
        task_type: str = "general",
        continuation_depth: int = 0,
        max_continuation_depth: int = 5,
        verification_depth: int = 0,
        max_verification_depth: int = 1,
        continuation_budget: int = 5,
        verification_for: str | None = None,
        is_terminal: bool = False,
        terminal_reason: str | None = None,
        requires_approval: bool = False,
        approval_reason: str | None = None,
    ) -> Task:
        t_id = task_id or f"task-{uuid.uuid4().hex[:8]}"
        task = Task(
            task_id=t_id,
            title=title,
            description=description,
            status=TaskStatus.READY,
            priority=priority,
            complexity=complexity,
            assigned_agent=assigned_agent,
            assigned_account=assigned_account,
            assigned_model=assigned_model,
            dependencies=dependencies or [],
            parent_task=parent_task,
            task_type=task_type,
            continuation_depth=continuation_depth,
            max_continuation_depth=max_continuation_depth,
            verification_depth=verification_depth,
            max_verification_depth=max_verification_depth,
            continuation_budget=continuation_budget,
            verification_for=verification_for,
            is_terminal=is_terminal,
            terminal_reason=terminal_reason,
            requires_approval=requires_approval,
            approval_reason=approval_reason,
        )
        self.save_task(task)
        return task

    def save_task(self, task: Task) -> Path:
        target_dir = self._dir_for_status(task.status)
        target_file = target_dir / f"{task.task_id}.json"
        payload = json.dumps(task.to_dict(), indent=2)

        with self._lock:
            # Write the new record first, atomically. Deleting the old copy first
            # (as before) meant a failed or interrupted write lost the task
            # entirely, and a concurrent reader could see a half-written file.
            _write_text_atomic(target_file, payload)

            # Then remove stale copies from the other directories.
            for d in (self._dir_queue, self._dir_active, self._dir_completed, self._dir_failed):
                if d != target_dir:
                    (d / f"{task.task_id}.json").unlink(missing_ok=True)
        return target_file

    def get_task(self, task_id: str) -> Task | None:
        filename = f"{task_id}.json"
        for d in (self._dir_queue, self._dir_active, self._dir_completed, self._dir_failed):
            file_path = d / filename
            if file_path.exists():
                try:
                    data = json.loads(file_path.read_text(encoding="utf-8"))
                    return Task.from_dict(data)
                except Exception:
                    return None

        # Check shared brain swarm tasks directory
        if self._include_swarm:
            swarm_dir = _brain_dir() / "swarm" / "tasks"
            if swarm_dir.is_dir():
                # Folder names are the on-disk names used by scripts/brain/swarm.py,
                # which writes "in-progress" with a hyphen. An underscore here
                # silently matched no directory, so running swarm tasks were
                # invisible to the dashboard.
                status_map = {
                    "pending": TaskStatus.READY,
                    "in-progress": TaskStatus.RUNNING,
                    "completed": TaskStatus.COMPLETED,
                    "escalated": TaskStatus.FAILED,
                }
                for folder, st in status_map.items():
                    p = swarm_dir / folder / filename
                    if p.exists():
                        try:
                            raw = json.loads(p.read_text(encoding="utf-8"))
                            t_id = raw.get("id") or raw.get("task_id", task_id)
                            title = raw.get("title") or raw.get("instruction") or raw.get("prompt") or "Untitled Task"
                            return Task(
                                task_id=t_id,
                                title=title,
                                description=raw.get("description") or "",
                                status=st,
                                assigned_agent=raw.get("assigned_to") or raw.get("assigned_agent"),
                                assigned_model=raw.get("model") or raw.get("assigned_model"),
                                actual_model=raw.get("model"),
                                created_at=raw.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                started_at=raw.get("started_at"),
                                completed_at=raw.get("completed_at"),
                                duration_seconds=float(raw.get("duration_seconds", 0.0) or 0.0),
                                result={"response": raw.get("output", "")} if "output" in raw else raw.get("result", {}),
                                errors=[raw["error"]] if raw.get("error") else [],
                            )
                        except Exception:
                            return None
        return None

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        actual_model: str | None = None,
        requested_model: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        duration_seconds: float | None = None,
        handoff: str | None = None,
        memory_refs: list[str] | None = None,
        is_terminal: bool | None = None,
        terminal_reason: str | None = None,
        task_type: str | None = None,
        continuation_depth: int | None = None,
        verification_depth: int | None = None,
        continuation_budget: int | None = None,
        verification_for: str | None = None,
        stage: str | None = None,
    ) -> Task | None:
        with self._lock:
            return self._update_status_locked(
                task_id, status, result=result, error=error, actual_model=actual_model,
                requested_model=requested_model, session_id=session_id,
                conversation_id=conversation_id, duration_seconds=duration_seconds,
                handoff=handoff, memory_refs=memory_refs, is_terminal=is_terminal,
                terminal_reason=terminal_reason, task_type=task_type,
                continuation_depth=continuation_depth, verification_depth=verification_depth,
                continuation_budget=continuation_budget, verification_for=verification_for,
                stage=stage,
            )

    def _update_status_locked(
        self,
        task_id: str,
        status: TaskStatus,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        actual_model: str | None = None,
        requested_model: str | None = None,
        session_id: str | None = None,
        conversation_id: str | None = None,
        duration_seconds: float | None = None,
        handoff: str | None = None,
        memory_refs: list[str] | None = None,
        is_terminal: bool | None = None,
        terminal_reason: str | None = None,
        task_type: str | None = None,
        continuation_depth: int | None = None,
        verification_depth: int | None = None,
        continuation_budget: int | None = None,
        verification_for: str | None = None,
        stage: str | None = None,
    ) -> Task | None:
        task = self.get_task(task_id)
        if not task:
            return None

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task.status = status

        if stage is not None:
            task.stage = stage
        elif status == TaskStatus.READY:
            task.stage = "QUEUED"
        elif status == TaskStatus.RUNNING and task.stage in ("QUEUED", "PLANNING"):
            task.stage = "EXECUTION"
        elif status in (TaskStatus.COMPLETED, TaskStatus.VERIFICATION_COMPLETE):
            task.stage = "COMPLETE"
        elif status in (
            TaskStatus.FAILED,
            TaskStatus.DEPTH_LIMIT_REACHED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.REJECTED,
            TaskStatus.CANCELLED,
        ):
            task.stage = "FAILED"

        if status == TaskStatus.RUNNING and not task.started_at:
            task.started_at = now
        elif status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.VERIFICATION_COMPLETE,
            TaskStatus.DEPTH_LIMIT_REACHED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.REJECTED,
        ):
            task.completed_at = now

        if is_terminal is not None:
            task.is_terminal = is_terminal
        elif status in (
            TaskStatus.CANCELLED,
            TaskStatus.VERIFICATION_COMPLETE,
            TaskStatus.DEPTH_LIMIT_REACHED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.REJECTED,
        ):
            task.is_terminal = True

        if terminal_reason is not None:
            task.terminal_reason = terminal_reason
        elif status in (
            TaskStatus.VERIFICATION_COMPLETE,
            TaskStatus.DEPTH_LIMIT_REACHED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.REJECTED,
        ):
            task.terminal_reason = status.value

        if task_type is not None:
            task.task_type = task_type
        if continuation_depth is not None:
            task.continuation_depth = continuation_depth
        if verification_depth is not None:
            task.verification_depth = verification_depth
        if continuation_budget is not None:
            task.continuation_budget = continuation_budget
        if verification_for is not None:
            task.verification_for = verification_for

        if actual_model:
            task.actual_model = actual_model
        if requested_model:
            task.requested_model = requested_model
        if session_id:
            task.session_id = session_id
        if conversation_id:
            task.conversation_id = conversation_id
        if duration_seconds is not None:
            task.duration_seconds = duration_seconds
        if handoff:
            task.handoffs.append(handoff)
        if memory_refs:
            task.memory_refs.extend(memory_refs)
        if result:
            task.result.update(result)
        if error:
            task.errors.append(error)

        self.save_task(task)
        return task

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """List tasks, newest first.

        Several statuses share a directory (BLOCKED, FAILED and CANCELLED all
        live in `failed/`), so the status filter is applied to the task record
        itself rather than inferred from its location.
        """
        dirs = (
            [self._dir_for_status(status)]
            if status
            else [self._dir_queue, self._dir_active, self._dir_completed, self._dir_failed]
        )
        tasks = []
        seen_ids = set()
        for d in dirs:
            for p in d.glob("*.json"):
                try:
                    task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if status and task.status != status:
                    continue
                if task.task_id not in seen_ids:
                    seen_ids.add(task.task_id)
                    tasks.append(task)

        # Also incorporate tasks from the shared brain swarm task pool
        if self._include_swarm:
            swarm_dir = _brain_dir() / "swarm" / "tasks"
            if swarm_dir.is_dir():
                # "in-progress" with a hyphen is the directory name that
                # scripts/brain/swarm.py actually writes; see the note above.
                status_folder_map = {
                    "pending": TaskStatus.READY,
                    "in-progress": TaskStatus.RUNNING,
                    "completed": TaskStatus.COMPLETED,
                    "escalated": TaskStatus.FAILED,
                }
                folders = (
                    [k for k, v in status_folder_map.items() if v == status]
                    if status
                    else ["pending", "in-progress", "completed", "escalated"]
                )
                for folder in folders:
                    folder_path = swarm_dir / folder
                    if not folder_path.is_dir():
                        continue
                    for p in folder_path.glob("*.json"):
                        try:
                            raw = json.loads(p.read_text(encoding="utf-8"))
                            t_id = raw.get("id") or raw.get("task_id")
                            if not t_id or t_id in seen_ids:
                                continue
                            seen_ids.add(t_id)
                            st = status_folder_map.get(folder, TaskStatus.COMPLETED)
                            title = raw.get("title") or raw.get("instruction") or raw.get("prompt") or "Untitled Task"
                            task = Task(
                                task_id=t_id,
                                title=title,
                                description=raw.get("description") or "",
                                status=st,
                                assigned_agent=raw.get("assigned_to") or raw.get("assigned_agent"),
                                assigned_model=raw.get("model") or raw.get("assigned_model"),
                                actual_model=raw.get("model"),
                                created_at=raw.get("created_at") or datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                started_at=raw.get("started_at"),
                                completed_at=raw.get("completed_at"),
                                duration_seconds=float(raw.get("duration_seconds", 0.0) or 0.0),
                                result={"response": raw.get("output", "")} if "output" in raw else raw.get("result", {}),
                                errors=[raw["error"]] if raw.get("error") else [],
                            )
                            if status and task.status != status:
                                continue
                            tasks.append(task)
                        except Exception:
                            continue

        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def recover_orphaned_tasks(self) -> int:
        """Move uncompleted tasks from active back to queue upon system restart."""
        count = 0
        for p in self._dir_active.glob("*.json"):
            try:
                task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
                task.status = TaskStatus.READY
                task.errors.append("Task was interrupted by system shutdown/restart and recovered.")
                self.save_task(task)
                count += 1
            except Exception:
                pass
        return count

    def reconcile_runtime_state(
        self, active_task_ids: set[str] | list[str] | None = None
    ) -> dict[str, int]:
        active_ids = set(active_task_ids or [])
        checked = 0
        still_running = 0
        completed = 0
        failed = 0
        recovered = 0

        # Scan all JSON records in the active/ directory
        for p in list(self._dir_active.glob("*.json")):
            checked += 1
            try:
                task = Task.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue

            # CASE A: Real worker thread or process is actively running in the swarm
            if task.task_id in active_ids:
                still_running += 1
                continue

            # Process is not alive in the active worker pool. Reconcile based on records:
            has_success_result = bool(task.result and task.result.get("success") is True)
            has_handoff = bool(task.handoffs and len(task.handoffs) > 0)
            has_errors = bool(task.errors and len(task.errors) > 0)

            # CASE B: Successful result exists or completed handoff exists without errors
            if has_success_result or (has_handoff and not has_errors):
                task.status = TaskStatus.COMPLETED
                task.stage = "COMPLETE"
                task.is_terminal = True
                task.terminal_reason = "COMPLETED"
                if not task.completed_at:
                    task.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.save_task(task)
                completed += 1
            # CASE C: Explicit failure recorded
            elif bool(task.result and task.result.get("success") is False) or has_errors:
                task.status = TaskStatus.FAILED
                task.stage = "FAILED"
                task.is_terminal = True
                task.terminal_reason = "FAILED"
                if not task.completed_at:
                    task.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.save_task(task)
                failed += 1
            # CASE D & E: Process is dead / vanished with no result
            else:
                task.status = TaskStatus.FAILED
                task.stage = "FAILED"
                task.is_terminal = True
                task.terminal_reason = "PROCESS_TERMINATED"
                task.errors.append(
                    "Process terminated or system restarted before task completed (recovered by runtime reconciler)"
                )
                if not task.completed_at:
                    task.completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.save_task(task)
                recovered += 1

        # Emit TASK_RECONCILED summary event if any tasks were reconciled
        try:
            from events.bus import event_bus, EventType
            if completed or failed or recovered:
                event_bus.publish(
                    EventType.TASK_RECONCILED,
                    metadata={
                        "checked": checked,
                        "still_running": still_running,
                        "completed": completed,
                        "failed": failed,
                        "recovered": recovered,
                    },
                )
        except Exception:
            pass

        return {
            "checked": checked,
            "still_running": still_running,
            "completed": completed,
            "failed": failed,
            "recovered": recovered,
        }
