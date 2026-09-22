"""Scoped, Persistent Shared Memory Store with SQLite Backing.

Supports GLOBAL, PROJECT, TASK, AGENT, and SESSION scopes with tag indexing
and relevance filtering. Excludes secrets and personal credentials.
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


class MemoryScope(str, Enum):
    GLOBAL = "GLOBAL"
    PROJECT = "PROJECT"
    TASK = "TASK"
    AGENT = "AGENT"
    SESSION = "SESSION"


@dataclass
class MemoryEntry:
    memory_id: str
    content: str
    scope: MemoryScope = MemoryScope.PROJECT
    source_agent: str = "unknown"
    task_id: str | None = None
    session_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    confidence: float = 1.0
    importance: int = 3  # 1 (low) to 5 (critical)
    tags: list[str] = field(default_factory=list)
    provenance: str = "system"

    @property
    def timestamp(self) -> str:
        return self.created_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["scope"] = self.scope.value
        d["timestamp"] = self.created_at
        return d

    @classmethod
    def from_row(cls, row: tuple) -> MemoryEntry:
        return cls(
            memory_id=row[0],
            content=row[1],
            scope=MemoryScope(row[2]),
            source_agent=row[3],
            task_id=row[4],
            session_id=row[5],
            created_at=row[6],
            confidence=row[7],
            importance=row[8],
            tags=json.loads(row[9]) if row[9] else [],
            provenance=row[10] or "system",
        )


class MemoryStore:
    """SQLite-backed shared agent memory store.

    One store is shared by every agent. No agent, including Antigravity
    Account 2, keeps private memory.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (Path(__file__).resolve().parent / "shared_memory.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    #: Pragmas applied to every connection.
    #:
    #: Without these, a plain connect() under the threaded dashboard plus separate
    #: agent CLI processes hits "database is locked" on concurrent writes and the
    #: INSERT is lost, because nothing retries. WAL lets readers proceed during a
    #: write; busy_timeout turns a writer collision into a short wait instead of an
    #: immediate error. synchronous=NORMAL is durable under WAL for process crashes
    #: (only a host power loss can lose the tail), which is the right trade for a
    #: local-first store written on every agent step.
    _PRAGMAS = (
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=5000",
        "PRAGMA synchronous=NORMAL",
    )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Connection that is always committed and always closed."""
        # check_same_thread=False is safe here because each call opens and closes its
        # own connection; it is set explicitly so a threaded caller cannot trip on it.
        conn = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        try:
            for pragma in self._PRAGMAS:
                conn.execute(pragma)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    task_id TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    importance INTEGER NOT NULL,
                    tags TEXT,
                    provenance TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scope ON memories(scope)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task ON memories(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_agent ON memories(source_agent)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON memories(created_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_retrievals (
                    retrieval_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    query TEXT NOT NULL,
                    retrieved_memory_id TEXT NOT NULL,
                    retrieval_score REAL NOT NULL,
                    agent_id TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_task ON memory_retrievals(task_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_mem ON memory_retrievals(retrieved_memory_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_time ON memory_retrievals(created_at)")

    def add(
        self,
        content: str,
        scope: MemoryScope = MemoryScope.PROJECT,
        source_agent: str = "unknown",
        task_id: str | None = None,
        session_id: str | None = None,
        importance: int = 3,
        tags: list[str] | None = None,
        provenance: str = "system",
    ) -> MemoryEntry:
        mem_id = f"mem-{uuid.uuid4().hex[:8]}"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry = MemoryEntry(
            memory_id=mem_id,
            content=content,
            scope=scope,
            source_agent=source_agent,
            task_id=task_id,
            session_id=session_id,
            created_at=now,
            importance=importance,
            tags=tags or [],
            provenance=provenance,
        )
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO memories (
                    memory_id, content, scope, source_agent, task_id, session_id,
                    created_at, confidence, importance, tags, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.memory_id,
                entry.content,
                entry.scope.value,
                entry.source_agent,
                entry.task_id,
                entry.session_id,
                entry.created_at,
                entry.confidence,
                entry.importance,
                json.dumps(entry.tags),
                entry.provenance,
            ))
        return entry

    def record_retrieval(
        self,
        query: str,
        retrieved_memory_id: str,
        retrieval_score: float,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        ret_id = f"ret-{uuid.uuid4().hex[:8]}"
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO memory_retrievals (
                    retrieval_id, task_id, query, retrieved_memory_id, retrieval_score, agent_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ret_id, task_id, query, retrieved_memory_id, retrieval_score, agent_id, now))
        return ret_id

    def list_retrievals(self, limit: int = 100, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if task_id:
                cursor = conn.execute(
                    "SELECT * FROM memory_retrievals WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                    (task_id, limit),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM memory_retrievals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            rows = cursor.fetchall()
            return [
                {
                    "retrieval_id": r[0],
                    "task_id": r[1],
                    "query": r[2],
                    "retrieved_memory_id": r[3],
                    "retrieval_score": round(r[4], 2),
                    "agent_id": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]

    def get_memory(self, memory_id: str) -> MemoryEntry | None:
        with self._connect() as conn:
            cursor = conn.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
            row = cursor.fetchone()
            return MemoryEntry.from_row(row) if row else None

    def query_memories(
        self,
        search: str | None = None,
        scope: MemoryScope | None = None,
        min_importance: int | None = None,
        task_id: str | None = None,
        source_agent: str | None = None,
        limit: int = 100,
    ) -> list[MemoryEntry]:
        query_parts = ["SELECT * FROM memories WHERE 1=1"]
        params: list[Any] = []

        if scope:
            query_parts.append("AND scope = ?")
            params.append(scope.value)
        if min_importance is not None:
            query_parts.append("AND importance >= ?")
            params.append(min_importance)
        if task_id:
            query_parts.append("AND task_id = ?")
            params.append(task_id)
        if source_agent:
            query_parts.append("AND source_agent = ?")
            params.append(source_agent)
        if search:
            query_parts.append("AND (content LIKE ? OR tags LIKE ?)")
            term = f"%{search}%"
            params.extend([term, term])

        query_parts.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)

        with self._connect() as conn:
            cursor = conn.execute(" ".join(query_parts), params)
            return [MemoryEntry.from_row(r) for r in cursor.fetchall()]

    def list_all(self, limit: int = 100) -> list[MemoryEntry]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            return [MemoryEntry.from_row(r) for r in cursor.fetchall()]

    def list_by_agent(self, source_agent: str, limit: int = 50) -> list[MemoryEntry]:
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT * FROM memories WHERE source_agent = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (source_agent, limit),
            )
            return [MemoryEntry.from_row(r) for r in cursor.fetchall()]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT count(*) FROM memories").fetchone()[0]
