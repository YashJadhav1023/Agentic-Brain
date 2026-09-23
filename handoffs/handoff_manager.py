"""Structured Agent-to-Agent Handoff Manager.

Produces, validates and archives standardized handoff records. Each handoff is
written twice: a human-readable Markdown file and a machine-readable JSON
sidecar so that Universal Continue can resume structurally instead of scraping
prose. There is exactly one handoff store — no agent keeps its own.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HandoffRecord:
    task: str
    objective: str
    completed: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    relevant_memory: list[str] = field(default_factory=list)
    git_state: str = "clean"
    remaining_work: list[str] = field(default_factory=list)
    next_action: str = ""
    recommended_agent: str = "antigravity-account-1"
    recommended_model: str = "auto"
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    # --- Execution identity, required for session-aware continuation --------
    task_id: str | None = None
    agent_id: str | None = None
    account_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    # --- Phase 4A Continuation & Verification Contract Metadata ------------
    parent_task_id: str | None = None
    task_type: str = "general"               # "general", "verification", "remediation"
    continuation_depth: int = 0
    max_continuation_depth: int = 5
    verification_depth: int = 0
    max_verification_depth: int = 1
    continuation_budget: int = 5
    is_terminal: bool = False
    terminal_reason: str | None = None
    verification_for: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_markdown(self) -> str:
        lines = [
            f"# Handoff: {self.task}",
            f"**Date:** {self.created_at}",
            f"**Objective:** {self.objective}",
            f"**Task ID:** `{self.task_id or 'n/a'}`",
            f"**Executed By:** `{self.agent_id or 'n/a'}` (account `{self.account_id or 'n/a'}`)",
            f"**Session:** `{self.session_id or 'n/a'}`",
            f"**Conversation:** `{self.conversation_id or 'n/a'}`",
            f"**Git State:** {self.git_state}",
            f"**Recommended Agent:** `{self.recommended_agent}`",
            f"**Recommended Model:** `{self.recommended_model}`",
            f"**Task Type:** `{self.task_type}`",
            f"**Continuation Depth:** `{self.continuation_depth} / {self.max_continuation_depth}`",
            f"**Verification Depth:** `{self.verification_depth} / {self.max_verification_depth}`",
            f"**Remaining Budget:** `{self.continuation_budget}`",
            f"**Terminal:** `{self.is_terminal}` ({self.terminal_reason or 'in-flight'})",
            "",
            "## Completed Work",
        ]
        for item in (self.completed or ["None reported"]):
            lines.append(f"- {item}")

        for heading, values in (
            ("Files Modified", self.files_modified),
            ("Tests Run", self.tests),
            ("Errors Encountered", self.errors),
            ("Decisions Made", self.decisions),
            ("Relevant Memory", self.relevant_memory),
            ("Remaining Work", self.remaining_work),
        ):
            lines.extend(["", f"## {heading}"])
            for item in (values or ["None"]):
                lines.append(f"- {item}")

        lines.extend(["", "## Next Action", self.next_action or "None"])
        return "\n".join(lines)

    def compress_for_prompt(self, max_chars: int = 2000) -> str:
        """Produce a concise, token-efficient handoff block for model context."""
        lines = [
            f"### Prior Task Handoff: {self.task}",
            f"- Objective: {self.objective[:200]}",
            f"- Completed: {'; '.join(self.completed[:3]) if self.completed else 'None'}",
        ]
        if self.files_modified:
            lines.append(f"- Files Modified: {', '.join(self.files_modified[:5])}")
        if self.remaining_work:
            lines.append(f"- Remaining Work: {'; '.join(self.remaining_work[:3])}")
        if self.next_action:
            lines.append(f"- Next Action: {self.next_action[:300]}")
        raw = "\n".join(lines)
        if len(raw) > max_chars:
            raw = raw[:max_chars].rstrip() + "..."
        return raw


class HandoffManager:
    """Manages reading, writing and archiving handoff records."""

    def __init__(self, root_dir: Path | None = None) -> None:
        self._root = root_dir or Path(__file__).resolve().parent
        self._root.mkdir(parents=True, exist_ok=True)
        self._current_file = self._root / "current.md"
        self._current_json = self._root / "current.json"
        self._archive_dir = self._root / "archive"
        self._archive_dir.mkdir(parents=True, exist_ok=True)

    def write_handoff(self, record: HandoffRecord) -> Path:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        if self._current_file.exists():
            self._current_file.rename(self._archive_dir / f"handoff_{timestamp}.md")
        if self._current_json.exists():
            self._current_json.rename(self._archive_dir / f"handoff_{timestamp}.json")

        self._current_file.write_text(record.to_markdown(), encoding="utf-8")
        self._current_json.write_text(
            json.dumps(record.to_dict(), indent=2), encoding="utf-8"
        )
        return self._current_file

    def get_current_handoff(self) -> str | None:
        if self._current_file.exists():
            return self._current_file.read_text(encoding="utf-8")
        return None

    def get_current_record(self) -> HandoffRecord | None:
        """Structured view of the latest handoff, or None if unavailable."""
        if not self._current_json.exists():
            return None
        try:
            return HandoffRecord.from_dict(
                json.loads(self._current_json.read_text(encoding="utf-8"))
            )
        except Exception:
            return None

    def get_compressed_handoff(self, max_chars: int = 2000) -> str:
        """Retrieve token-bounded handoff context for prompt assembly."""
        record = self.get_current_record()
        if record:
            return record.compress_for_prompt(max_chars=max_chars)
        raw = self.get_current_handoff()
        if not raw:
            return ""
        return raw[:max_chars].rstrip() + ("..." if len(raw) > max_chars else "")

    def list_archive(self, limit: int = 20) -> list[Path]:
        files = sorted(
            self._archive_dir.glob("handoff_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return files[:limit]

    def list_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """List historical handoffs from archive and current handoff, newest first."""
        records: list[dict[str, Any]] = []
        current = self.get_current_record()
        if current:
            d = current.to_dict()
            d["is_current"] = True
            d["filename"] = "current.json"
            records.append(d)

        json_files = sorted(
            self._archive_dir.glob("handoff_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in json_files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                data["is_current"] = False
                data["filename"] = p.name
                records.append(data)
            except Exception:
                continue
            if len(records) >= limit:
                break

        return records[:limit]

    def get_record_by_name(self, filename: str) -> dict[str, Any] | None:
        """Get specific handoff record by filename ('current.json' or 'handoff_...json')."""
        if filename == "current.json":
            rec = self.get_current_record()
            return rec.to_dict() if rec else None
        # The name reaches here from an HTTP query string, so it must address a
        # file directly inside the archive: no separators, no "..", and the
        # resolved path must stay under the archive (symlinks included).
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or filename in (".", "..")
            or not filename.endswith(".json")
        ):
            return None
        archive_root = self._archive_dir.resolve()
        target = (archive_root / filename).resolve()
        if not target.is_relative_to(archive_root) or target.parent != archive_root:
            return None
        if target.is_file():
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None
