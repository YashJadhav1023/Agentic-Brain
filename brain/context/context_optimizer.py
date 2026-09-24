"""Context and Token Optimizer (Phase 4B).

Dramatically reduces unnecessary model context, eliminates duplicate content,
enforces strict context budgets, scopes target files, and provides honest token telemetry.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from events.bus import EventBus, EventType


# Common code and data extensions
FILE_PATH_PATTERN = re.compile(
    r"(?:[\w\-./\\]+)?\b[\w\-]+\.(?:py|json|md|ya?ml|sh|toml|html|css|jsx?|tsx?|txt|ini|cfg|sql|go|rs|java|c|cpp|h)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextBudget:
    """Configurable token and character bounds for context components."""
    max_memory_chars: int = 1500
    max_handoff_chars: int = 2000
    max_file_context_chars: int = 4000
    max_history_chars: int = 1000
    max_total_prompt_chars: int = 12000


@dataclass
class OptimizationReport:
    """Telemetry report detailing context savings and budget enforcement."""
    task_id: str
    target_files: list[str] = field(default_factory=list)
    raw_prompt_chars: int = 0
    optimized_prompt_chars: int = 0
    chars_saved: int = 0
    reduction_percentage: float = 0.0
    truncations: dict[str, bool] = field(default_factory=dict)
    duplicates_removed: int = 0
    is_lightweight: bool = False
    options_applied: dict[str, Any] = field(default_factory=dict)


class TargetFileDetector:
    """Deterministically extracts candidate target files from instructions and metadata."""

    @staticmethod
    def detect(task_text: str, declared_files: list[str] | None = None, workspace: Path | None = None) -> list[str]:
        found: set[str] = set(declared_files or [])

        # Extract file paths from task text using regex
        matches = FILE_PATH_PATTERN.findall(task_text)
        for match in matches:
            cleaned = match.strip("`'\" \t\r\n")
            if cleaned and not cleaned.startswith("http"):
                found.add(cleaned)

        # If workspace provided, check if paths actually exist relative to workspace
        if workspace and workspace.is_dir():
            validated: set[str] = set()
            for f in found:
                candidate_path = workspace / f if not os.path.isabs(f) else Path(f)
                if candidate_path.exists():
                    try:
                        rel = candidate_path.relative_to(workspace)
                        validated.add(str(rel))
                    except ValueError:
                        validated.add(str(candidate_path))
                else:
                    validated.add(f)
            return sorted(validated)

        return sorted(found)


class ContextDeduplicator:
    """Eliminates redundant text blocks across memory, handoff, and instructions."""

    @staticmethod
    def deduplicate_lines(text: str) -> tuple[str, int]:
        lines = text.splitlines()
        seen: set[str] = set()
        unique_lines: list[str] = []
        removed = 0

        for line in lines:
            stripped = line.strip()
            # Retain empty lines for formatting
            if not stripped:
                unique_lines.append(line)
                continue

            line_hash = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            if line_hash in seen and len(stripped) > 20:
                removed += 1
                continue
            seen.add(line_hash)
            unique_lines.append(line)

        return "\n".join(unique_lines), removed


class ContextOptimizer:
    """Coordinates workspace scoping, context budgets, and deduplication."""

    def __init__(self, budget: ContextBudget | None = None, event_bus: EventBus | None = None) -> None:
        self._budget = budget or ContextBudget()
        self._event_bus = event_bus

    @property
    def budget(self) -> ContextBudget:
        return self._budget

    def truncate_safely(self, text: str, max_chars: int, section_name: str) -> tuple[str, bool]:
        if len(text) <= max_chars:
            return text, False
        truncated = text[:max_chars].rstrip() + f"\n[... {section_name} truncated to {max_chars} chars by ContextOptimizer budget ...]"
        return truncated, True

    def optimize_prompt(
        self,
        task_id: str,
        title: str,
        description: str,
        target_files: list[str] | None = None,
        memory_block: str = "",
        handoff_block: str = "",
        complexity: str = "standard",
        workspace: Path | None = None,
    ) -> tuple[str, OptimizationReport]:
        """Produce a strictly bounded, scoped, and deduplicated prompt."""
        detected_files = TargetFileDetector.detect(f"{title} {description}", target_files, workspace)
        is_lightweight = complexity in ("fast", "low", "minimal") or any(
            w in f"{title} {description}".lower()
            for w in ("quick fix", "typo", "lint", "format", "ping", "test probe", "verify output")
        )

        truncations: dict[str, bool] = {}

        # 1. Truncate memory within budget
        opt_memory, trunc_mem = self.truncate_safely(memory_block, self._budget.max_memory_chars, "Memory Context")
        truncations["memory"] = trunc_mem

        # 2. Truncate handoff within budget
        opt_handoff, trunc_ho = self.truncate_safely(handoff_block, self._budget.max_handoff_chars, "Handoff Context")
        truncations["handoff"] = trunc_ho

        # 3. Assemble raw prompt parts
        parts: list[str] = [f"Task: {title}", "", "Description:", description]

        # 4. Inject scoped target files directive if applicable
        if detected_files:
            file_list_str = "\n".join(f"- {f}" for f in detected_files)
            parts.extend([
                "",
                "[TARGET FILES SCOPE]",
                "Operate strictly on the following target files:",
                file_list_str,
                "Do NOT scan the entire repository or read unrelated modules.",
            ])

        if opt_memory:
            parts.extend(["", opt_memory])

        if opt_handoff:
            parts.extend(["", opt_handoff])

        raw_assembled = "\n".join(parts)
        raw_chars = len(raw_assembled)

        # 5. Deduplicate redundant lines across sections
        deduped, dupes_removed = ContextDeduplicator.deduplicate_lines(raw_assembled)

        # 6. Final safety cap
        final_prompt, total_trunc = self.truncate_safely(deduped, self._budget.max_total_prompt_chars, "Total Prompt")
        truncations["total_prompt"] = total_trunc
        final_chars = len(final_prompt)

        saved = max(0, raw_chars - final_chars)
        red_pct = (saved / raw_chars * 100.0) if raw_chars > 0 else 0.0

        # CLI options for lightweight execution
        options_applied: dict[str, Any] = {}
        if is_lightweight:
            options_applied["disable_slash_commands"] = True
            options_applied["effort"] = "low"

        report = OptimizationReport(
            task_id=task_id,
            target_files=detected_files,
            raw_prompt_chars=raw_chars,
            optimized_prompt_chars=final_chars,
            chars_saved=saved,
            reduction_percentage=round(red_pct, 2),
            truncations=truncations,
            duplicates_removed=dupes_removed,
            is_lightweight=is_lightweight,
            options_applied=options_applied,
        )

        if self._event_bus:
            self._event_bus.publish(
                EventType.CONTEXT_OPTIMIZED,
                task_id=task_id,
                metadata={
                    "raw_chars": raw_chars,
                    "optimized_chars": final_chars,
                    "saved_chars": saved,
                    "reduction_pct": report.reduction_percentage,
                    "is_lightweight": is_lightweight,
                    "target_files_count": len(detected_files),
                },
            )

        return final_prompt, report


@dataclass
class TokenRecord:
    """Structured, honest token and duration metric for a single task."""
    task_id: str
    agent_id: str
    account_id: str
    provider: str
    requested_model: str | None
    actual_model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    duration_seconds: float = 0.0
    status: str = "known"  # 'known', 'estimated', 'unknown'
    usage_source: str = "unknown"  # 'reported', 'parsed', 'estimated', 'unknown'
    task_type: str = "normal"
    complexity: str = "standard"
    success: bool = True
    started_at: str | None = None
    completed_at: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())

    @property
    def model(self) -> str:
        return self.actual_model

    @property
    def token_reporting(self) -> str:
        """Alias for status tier: 'known', 'estimated', 'unknown'."""
        return self.status

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["model"] = self.actual_model
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class TokenTelemetryTracker:
    """Persistent tracker and aggregator for token metrics across all agents."""

    def __init__(self, log_path: Path | None = None, event_bus: EventBus | None = None) -> None:
        self._log_path = log_path or (Path(__file__).resolve().parent.parent.parent / "runtime" / "logs" / "token_telemetry.jsonl")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._event_bus = event_bus

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Heuristic calculation of token count (~4 characters per token)."""
        if not text:
            return 0
        return max(1, len(text) // 4)

    def record_usage(
        self,
        task_id: str,
        agent_id: str,
        account_id: str,
        provider: str,
        requested_model: str | None,
        actual_model: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        duration_seconds: float = 0.0,
        task_type: str = "normal",
        complexity: str = "standard",
        success: bool = True,
        usage_source: str = "unknown",
        started_at: str | None = None,
        completed_at: str | None = None,
        raw_response: dict[str, Any] | None = None,
    ) -> TokenRecord:
        """Record honest token telemetry. Never claims known metrics if unreported."""
        inp = input_tokens if input_tokens is not None else (0 if total_tokens is not None else None)
        out = output_tokens if output_tokens is not None else (0 if total_tokens is not None else None)
        tot = total_tokens

        # If input or output provided, compute total if total was None/0
        if tot is None and (inp is not None or out is not None):
            tot = (inp or 0) + (out or 0)

        # Determine honest status
        if usage_source == "estimated":
            status = "estimated"
        elif tot is not None and tot > 0:
            status = "known"
            if usage_source == "unknown":
                usage_source = "reported"
        else:
            status = "unknown"
            usage_source = "unknown"
            inp = None
            out = None
            tot = None

        record = TokenRecord(
            task_id=task_id,
            agent_id=agent_id,
            account_id=account_id,
            provider=provider,
            requested_model=requested_model,
            actual_model=actual_model,
            input_tokens=inp,
            output_tokens=out,
            total_tokens=tot,
            cache_read_tokens=cache_read_tokens,
            duration_seconds=round(duration_seconds, 3),
            status=status,
            usage_source=usage_source,
            task_type=task_type,
            complexity=complexity,
            success=success,
            started_at=started_at,
            completed_at=completed_at,
        )

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
        except Exception:
            pass

        if self._event_bus:
            self._event_bus.publish(
                EventType.TOKEN_USAGE_RECORDED,
                agent_id=agent_id,
                task_id=task_id,
                provider=provider,
                metadata={
                    "total_tokens": tot,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "status": status,
                    "usage_source": usage_source,
                    "duration_seconds": duration_seconds,
                    "actual_model": actual_model,
                },
            )

        return record

    def get_metrics(self, day: str | None = None) -> dict[str, Any]:
        """Aggregate token telemetry across agents, models, providers, and status tiers.

        ``day`` ("YYYY-MM-DD", UTC) limits the aggregate to records stamped that day.
        """
        records: list[TokenRecord] = []
        if self._log_path.is_file():
            try:
                with open(self._log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            records.append(TokenRecord.from_dict(json.loads(line)))
            except Exception:
                pass

        if day:
            records = [r for r in records if str(r.timestamp or "").startswith(day)]

        total_tasks = len(records)
        known_tokens = 0
        estimated_tokens = 0
        unknown_count = 0
        total_input = 0
        total_output = 0

        by_agent: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}

        for r in records:
            if r.status == "known" and r.total_tokens is not None:
                known_tokens += r.total_tokens
                if r.input_tokens is not None:
                    total_input += r.input_tokens
                if r.output_tokens is not None:
                    total_output += r.output_tokens
            elif r.status == "estimated" and r.total_tokens is not None:
                estimated_tokens += r.total_tokens
            else:
                unknown_count += 1

            # Group by provider
            p_key = r.provider or "unknown"
            if p_key not in by_provider:
                by_provider[p_key] = {
                    "tasks": 0,
                    "known_tokens": 0,
                    "estimated_tokens": 0,
                    "unknown_usage_runs": 0,
                    "total_duration": 0.0,
                }
            by_provider[p_key]["tasks"] += 1
            if r.status == "known" and r.total_tokens is not None:
                by_provider[p_key]["known_tokens"] += r.total_tokens
            elif r.status == "estimated" and r.total_tokens is not None:
                by_provider[p_key]["estimated_tokens"] += r.total_tokens
            else:
                by_provider[p_key]["unknown_usage_runs"] += 1
            by_provider[p_key]["total_duration"] += r.duration_seconds

            # Group by agent
            if r.agent_id not in by_agent:
                by_agent[r.agent_id] = {
                    "tasks": 0,
                    "known_tokens": 0,
                    "estimated_tokens": 0,
                    "unknown_usage_runs": 0,
                    "total_duration": 0.0,
                    "success_count": 0,
                }
            by_agent[r.agent_id]["tasks"] += 1
            if r.status == "known" and r.total_tokens is not None:
                by_agent[r.agent_id]["known_tokens"] += r.total_tokens
            elif r.status == "estimated" and r.total_tokens is not None:
                by_agent[r.agent_id]["estimated_tokens"] += r.total_tokens
            else:
                by_agent[r.agent_id]["unknown_usage_runs"] += 1
            by_agent[r.agent_id]["total_duration"] += r.duration_seconds
            if r.success:
                by_agent[r.agent_id]["success_count"] += 1

            # Group by model
            m_key = r.actual_model or "unknown"
            if m_key not in by_model:
                by_model[m_key] = {"tasks": 0, "known_tokens": 0, "estimated_tokens": 0, "unknown_usage_runs": 0}
            by_model[m_key]["tasks"] += 1
            if r.status == "known" and r.total_tokens is not None:
                by_model[m_key]["known_tokens"] += r.total_tokens
            elif r.status == "estimated" and r.total_tokens is not None:
                by_model[m_key]["estimated_tokens"] += r.total_tokens
            else:
                by_model[m_key]["unknown_usage_runs"] += 1

        # Compute averages
        for a_id, data in by_agent.items():
            tasks = data["tasks"]
            data["avg_tokens"] = round(data["known_tokens"] / tasks, 1) if tasks else 0
            data["avg_duration"] = round(data["total_duration"] / tasks, 2) if tasks else 0.0
            data["success_rate"] = round(data["success_count"] / tasks, 3) if tasks else 0.0

        return {
            "totals": {
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": known_tokens + estimated_tokens,
            },
            "by_provider": by_provider,
            "by_agent": by_agent,
            "by_model": by_model,
            "known_tokens": known_tokens,
            "estimated_tokens": estimated_tokens,
            "unknown_usage_runs": unknown_count,
            # Backwards-compatible aliases
            "total_tasks_recorded": total_tasks,
            "total_known_tokens": known_tokens,
            "unknown_metric_tasks": unknown_count,
        }
