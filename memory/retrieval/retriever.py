"""Relevance-Filtered Context Retrieval.

Extracts only the most relevant memories for a task to avoid bloating model context.
Combines keyword matching, tag overlap, and importance weighting.
"""
from __future__ import annotations

import datetime
import logging
import re
import sqlite3
from typing import Any

from events.bus import EventBus, EventType
from memory.store.memory_store import MemoryEntry, MemoryScope, MemoryStore

logger = logging.getLogger(__name__)


class MemoryRetriever:
    """Intelligent context retrieval for prompts."""

    def __init__(self, store: MemoryStore, event_bus: EventBus | None = None) -> None:
        self._store = store
        self._event_bus = event_bus

    def retrieve_context(
        self,
        query: str,
        scope: MemoryScope | None = None,
        task_id: str | None = None,
        max_items: int = 5,
        max_bytes: int = 2048,
    ) -> list[MemoryEntry]:
        """Retrieve top relevant memories matching query within byte budget."""
        if self._event_bus:
            self._event_bus.publish(
                EventType.MEMORY_RETRIEVAL_STARTED,
                task_id=task_id,
                metadata={"query": query[:200], "scope": scope.value if scope else "all"},
            )

        all_memories = self._store.list_all(limit=200)
        if not all_memories:
            if self._event_bus:
                self._event_bus.publish(
                    EventType.MEMORY_RETRIEVAL_COMPLETED,
                    task_id=task_id,
                    metadata={"query": query[:200], "retrieved_count": 0, "retrieved": []},
                )
            return []

        tokens = set(re.findall(r"\w{3,}", query.lower()))

        now = datetime.datetime.now(datetime.timezone.utc)
        SCOPE_WEIGHTS = {
            MemoryScope.TASK: 5.0,
            MemoryScope.SESSION: 4.0,
            MemoryScope.PROJECT: 2.0,
            MemoryScope.GLOBAL: 1.0,
            MemoryScope.AGENT: 1.5,
        }

        scored: list[tuple[float, MemoryEntry]] = []
        for mem in all_memories:
            # Filter scope if requested
            if scope and mem.scope != scope:
                continue

            score = float(mem.importance)

            # Scope hierarchy weight
            score += SCOPE_WEIGHTS.get(mem.scope, 1.0)

            # Exact task match bonus
            if task_id and mem.task_id == task_id:
                score += 5.0

            # Keyword matching
            mem_tokens = set(re.findall(r"\w{3,}", mem.content.lower()))
            overlap = tokens.intersection(mem_tokens)
            score += len(overlap) * 2.0

            # Tag match bonus
            for tag in mem.tags:
                if tag.lower() in tokens:
                    score += 3.0

            # Recency decay: newer memories get higher freshness multiplier
            try:
                mem_time_str = getattr(mem, "created_at", getattr(mem, "timestamp", None))
                if mem_time_str:
                    mem_time = datetime.datetime.fromisoformat(mem_time_str)
                    if mem_time.tzinfo is None:
                        mem_time = mem_time.replace(tzinfo=datetime.timezone.utc)
                    age_hours = max(0.0, (now - mem_time).total_seconds() / 3600.0)
                    recency_multiplier = max(0.5, 1.5 - min(1.0, age_hours / 72.0))
                    score *= recency_multiplier
            except Exception:
                pass

            scored.append((score, mem))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        selected: list[MemoryEntry] = []
        accumulated_bytes = 0
        retrieval_metadata: list[dict[str, Any]] = []

        for score, mem in scored[:max_items]:
            entry_bytes = len(mem.content.encode("utf-8"))
            if accumulated_bytes + entry_bytes > max_bytes and selected:
                break
            selected.append(mem)
            accumulated_bytes += entry_bytes
            retrieval_metadata.append({
                "memory_id": mem.memory_id,
                "score": round(score, 2),
                "scope": mem.scope.value,
                "importance": mem.importance,
            })
            if hasattr(self._store, "record_retrieval"):
                try:
                    self._store.record_retrieval(
                        query=query,
                        retrieved_memory_id=mem.memory_id,
                        retrieval_score=score,
                        task_id=task_id,
                        agent_id=mem.source_agent,
                    )
                except sqlite3.OperationalError as exc:
                    # Previously `except Exception: pass`, which silently dropped
                    # retrieval-history rows whenever the database was momentarily
                    # locked — the exact failure WAL and busy_timeout now make rare.
                    # A lost row must still be visible rather than invisible, so it is
                    # logged. Retrieval itself is not failed: the caller already has
                    # its context and losing an audit row must not break the read.
                    logger.warning(
                        "retrieval history not recorded for memory %s: %s",
                        mem.memory_id,
                        exc,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "unexpected error recording retrieval for memory %s: %s",
                        mem.memory_id,
                        exc,
                    )

        if self._event_bus:
            self._event_bus.publish(
                EventType.MEMORY_RETRIEVAL_COMPLETED,
                task_id=task_id,
                metadata={
                    "query": query[:200],
                    "count": len(selected),
                    "retrieved_count": len(selected),
                    "retrieved": retrieval_metadata,
                },
            )

        return selected

    def format_context_for_prompt(self, memories: list[MemoryEntry]) -> str:
        """Render a compact Markdown block for inclusion in an agent prompt."""
        if not memories:
            return ""
        lines = ["### Shared Brain Context:"]
        for m in memories:
            lines.append(f"- [{m.scope.value}] (Source: {m.source_agent}): {m.content}")
        return "\n".join(lines)
