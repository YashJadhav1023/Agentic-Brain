"""Structured Append-Only Event Bus and Telemetry.

Records all system and agent state changes into an append-only JSONL log.
Supports specific telemetry events for Antigravity Account 2.
"""
from __future__ import annotations

import datetime
import json
import threading
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class EventType(str, Enum):
    # Core Agent Lifecycle
    AGENT_STARTED = "AGENT_STARTED"
    AGENT_STOPPED = "AGENT_STOPPED"
    AGENT_HEARTBEAT = "AGENT_HEARTBEAT"
    AGENT_HEALTH = "AGENT_HEALTH"

    # Core Task Lifecycle
    TASK_CREATED = "TASK_CREATED"
    TASK_QUEUED = "TASK_QUEUED"
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_HANDOFF = "TASK_HANDOFF"
    TASK_RECONCILED = "TASK_RECONCILED"

    # Context & Routing
    ROUTE_STARTED = "ROUTE_STARTED"
    ROUTE_SELECTED = "ROUTE_SELECTED"
    MEMORY_CREATED = "MEMORY_CREATED"
    MEMORY_ACCESSED = "MEMORY_ACCESSED"
    MEMORY_RETRIEVAL_STARTED = "MEMORY_RETRIEVAL_STARTED"
    MEMORY_RETRIEVAL_COMPLETED = "MEMORY_RETRIEVAL_COMPLETED"
    MODEL_SELECTED = "MODEL_SELECTED"
    MODEL_SWITCHED = "MODEL_SWITCHED"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_FAILED = "TOOL_FAILED"
    FILE_LOCKED = "FILE_LOCKED"
    FILE_UNLOCKED = "FILE_UNLOCKED"
    GIT_EVENT = "GIT_EVENT"
    ERROR = "ERROR"

    # Approval gate (BUG-002 fix): destructive tasks must stop for operator
    # approval before any queue consumer may execute them.
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"

    # Dedicated Antigravity Account 2 Telemetry Events
    ANTIGRAVITY_ACCOUNT2_STARTED = "ANTIGRAVITY_ACCOUNT2_STARTED"
    ANTIGRAVITY_ACCOUNT2_COMPLETED = "ANTIGRAVITY_ACCOUNT2_COMPLETED"
    ANTIGRAVITY_ACCOUNT2_FAILED = "ANTIGRAVITY_ACCOUNT2_FAILED"
    ANTIGRAVITY_ACCOUNT2_HEALTH = "ANTIGRAVITY_ACCOUNT2_HEALTH"
    ANTIGRAVITY_ACCOUNT2_MODEL_SELECTED = "ANTIGRAVITY_ACCOUNT2_MODEL_SELECTED"
    ANTIGRAVITY_ACCOUNT2_HANDOFF = "ANTIGRAVITY_ACCOUNT2_HANDOFF"

    # Phase 3 Security & Audit Events
    AUTH_SUCCESS = "AUTH_SUCCESS"
    AUTH_FAILURE = "AUTH_FAILURE"
    TASK_EXECUTION_REQUESTED = "TASK_EXECUTION_REQUESTED"
    TASK_EXECUTION_STARTED = "TASK_EXECUTION_STARTED"
    TASK_EXECUTION_COMPLETED = "TASK_EXECUTION_COMPLETED"
    TASK_EXECUTION_FAILED = "TASK_EXECUTION_FAILED"
    PERMISSION_ESCALATION_REQUESTED = "PERMISSION_ESCALATION_REQUESTED"
    PERMISSION_ESCALATION_GRANTED = "PERMISSION_ESCALATION_GRANTED"
    PERMISSION_ESCALATION_DENIED = "PERMISSION_ESCALATION_DENIED"

    # Provider Explicit Lifecycle Events
    PROVIDER_STARTED = "PROVIDER_STARTED"
    PROVIDER_COMPLETED = "PROVIDER_COMPLETED"
    PROVIDER_FAILED = "PROVIDER_FAILED"

    # Phase 3 Worktree & Sandbox Events
    WORKTREE_CREATED = "WORKTREE_CREATED"
    WORKTREE_DESTROYED = "WORKTREE_DESTROYED"
    DIFF_APPROVED = "DIFF_APPROVED"
    DIFF_REJECTED = "DIFF_REJECTED"
    MERGE_APPLIED = "MERGE_APPLIED"

    # Phase 4A Continuation & Termination Events
    HANDOFF_CREATED = "HANDOFF_CREATED"
    CONTINUATION_STARTED = "CONTINUATION_STARTED"
    CONTINUATION_TERMINATED = "CONTINUATION_TERMINATED"
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    VERIFICATION_REJECTED = "VERIFICATION_REJECTED"
    RECURSION_PREVENTED = "RECURSION_PREVENTED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    DEPTH_LIMIT_REACHED = "DEPTH_LIMIT_REACHED"

    # Phase 4B & 4C Optimization & Failover Events
    TOKEN_USAGE_RECORDED = "TOKEN_USAGE_RECORDED"
    AGENT_FAILOVER_TRIGGERED = "AGENT_FAILOVER_TRIGGERED"
    FAILOVER_STARTED = "FAILOVER_STARTED"
    FAILOVER_COMPLETED = "FAILOVER_COMPLETED"
    CONTEXT_OPTIMIZED = "CONTEXT_OPTIMIZED"


@dataclass
class Event:
    event_type: EventType
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    event_id: str = field(default_factory=lambda: f"evt-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S%f')[:17]}")
    agent_id: str | None = None
    provider: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        d["payload"] = self.payload or self.metadata
        d["provider"] = self.provider or self.metadata.get("provider")
        return d


class EventBus:
    """Thread-safe event bus with persistent disk backing."""

    #: Rotate once the live log passes this size, keeping one previous generation
    #: as ``events.jsonl.1``. Matches the convention already used for
    #: ``routing_history.jsonl``, which had a ``.1`` while this log had none and
    #: had grown to 37 MiB unbounded. Two generations bound worst-case disk use
    #: at ~2x this value while still leaving a useful amount of history for
    #: replay.
    MAX_LOG_BYTES = 16 * 1024 * 1024

    #: How far back from the end of the file to read when answering
    #: get_recent_events(). Reading the whole file to return the last few hundred
    #: rows cost ~0.5 s on a 37 MiB log, on an endpoint the UI polls; the tail is
    #: the only part that can contain the newest events.
    TAIL_READ_BYTES = 4 * 1024 * 1024

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = log_path or (Path(__file__).resolve().parent.parent / "runtime" / "logs" / "events.jsonl")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[Event], None]] = []

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[Event], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    def _rotate_if_needed(self) -> None:
        """Move the live log aside once it exceeds MAX_LOG_BYTES.

        Called with the lock held, so one writer rotates and the others append to
        whatever file is current afterwards. Any failure here is swallowed: a log
        that cannot be rotated must still accept events, because losing telemetry
        is worse than an oversized file.
        """
        try:
            if self._log_path.stat().st_size < self.MAX_LOG_BYTES:
                return
            previous = self._log_path.with_suffix(self._log_path.suffix + ".1")
            self._log_path.replace(previous)
        except OSError:
            return

    def emit(self, event: Event) -> Event:
        line = json.dumps(event.to_dict()) + "\n"

        with self._lock:
            self._rotate_if_needed()
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
            for sub in self._subscribers:
                try:
                    sub(event)
                except Exception:
                    pass

        return event

    def publish(
        self,
        event_type: EventType,
        agent_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        provider: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        evt = Event(
            event_type=event_type,
            agent_id=agent_id,
            provider=provider or (metadata.get("provider") if metadata else None),
            task_id=task_id,
            session_id=session_id,
            metadata=metadata or {},
            payload=payload or metadata or {},
        )
        return self.emit(evt)

    def get_recent_events(self, limit: int = 50) -> list[Event]:
        """Return up to `limit` newest events, newest first.

        Reads only the tail of the file. The previous implementation called
        readlines() on the whole log and then sliced the last `limit` entries,
        which loaded 37 MiB into memory to return 400 rows and cost ~0.5 s per
        call on an endpoint the dashboard polls. Seeking to the last
        TAIL_READ_BYTES gives identical results for any sane `limit`, because the
        newest events are by construction at the end of an append-only log.
        """
        if not self._log_path.exists():
            return []
        events: list[Event] = []
        known_fields = {f.name for f in fields(Event)}
        try:
            with open(self._log_path, "rb") as fh:
                size = fh.seek(0, 2)
                start = max(0, size - self.TAIL_READ_BYTES)
                fh.seek(start)
                blob = fh.read()
        except OSError:
            return []
        text = blob.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # A non-zero offset almost certainly lands mid-line; that fragment is not
        # valid JSON and would be silently discarded below, but dropping it
        # explicitly keeps the intent clear.
        if start > 0 and lines:
            lines = lines[1:]
        for line in reversed(lines[-limit:] if limit > 0 else lines):
            try:
                data = json.loads(line)
                data["event_type"] = EventType(data["event_type"])
                filtered = {k: v for k, v in data.items() if k in known_fields}
                events.append(Event(**filtered))
            except Exception:
                pass
        return events
