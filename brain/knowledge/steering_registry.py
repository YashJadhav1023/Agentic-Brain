"""Steering & Instruction Document Registry with Precedence and Conflict Detection.

Discovers and organizes project, repository, and global AI steering instructions (e.g. AGENTS.md,
CLAUDE.md, copilot-instructions.md). Enforces hierarchical precedence (TASK > DIR > REPO > PROJECT > ORG > GLOBAL)
and detects conflicting rules instead of silently ignoring them.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

def _workspace_root() -> str:
    """Root to search for project docs/steering.

    Defaults to the current working directory so a fresh clone works anywhere;
    previously this was hardcoded to a single developer's directory layout.
    """
    return os.environ.get("BRAIN_WORKSPACE_ROOT") or os.getcwd()


def _workspace_name() -> str:
    """Name of the workspace root, used as the project/organization label."""
    return os.path.basename(os.path.normpath(_workspace_root())) or "workspace"


logger = logging.getLogger(__name__)


class SteeringScope(str, Enum):
    TASK = "TASK"
    DIRECTORY = "DIRECTORY"
    REPOSITORY = "REPOSITORY"
    PROJECT = "PROJECT"
    ORGANIZATION = "ORGANIZATION"
    GLOBAL = "GLOBAL"


# Default precedence scores for scopes (higher wins)
SCOPE_PRECEDENCE = {
    SteeringScope.TASK: 60,
    SteeringScope.DIRECTORY: 50,
    SteeringScope.REPOSITORY: 40,
    SteeringScope.PROJECT: 30,
    SteeringScope.ORGANIZATION: 20,
    SteeringScope.GLOBAL: 10,
}

# Recognized filename patterns for steering files
STEERING_FILENAMES = {
    "agents.md", "claude.md", "gemini.md", "kiro.md", "cline.md",
    "copilot-instructions.md", "instructions.md", "rules.md",
    "steering.md", "system_prompt.md", "ai_instructions.md"
}


@dataclass
class SteeringConflict:
    """Represents a conflict or contradiction detected between two steering directives."""
    source_a: str
    source_b: str
    conflicting_rule: str
    scope_a: str
    scope_b: str
    priority_a: int
    priority_b: int
    resolution: str  # e.g., "source_a overrides source_b by priority", "unresolved"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SteeringDocument:
    """Represents an approved instruction or steering document."""
    id: str
    path: str
    scope: SteeringScope
    project: str = ""
    priority: int = 30
    tags: List[str] = field(default_factory=list)
    summary: str = ""
    content_hash: str = ""
    modified_time: float = 0.0
    enabled: bool = True
    trust_level: str = "high"  # high, medium, low
    rules: List[str] = field(default_factory=list)
    raw_content: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["scope"] = self.scope.value if isinstance(self.scope, SteeringScope) else str(self.scope)
        # omit raw_content from brief dictionary to prevent huge payloads
        d.pop("raw_content", None)
        return d


class SteeringRegistry:
    """Registry managing discovered steering documents, precedence resolution, and conflict checks."""

    def __init__(self, search_roots: Optional[List[str]] = None) -> None:
        self.search_roots = search_roots or [
            _workspace_root(),
            os.path.expanduser("."),
        ]
        self._documents: Dict[str, SteeringDocument] = {}
        self._conflicts: List[SteeringConflict] = []

    def discover(self) -> List[SteeringDocument]:
        """Scan search roots for approved steering files."""
        discovered: Dict[str, SteeringDocument] = {}

        for root_dir in self.search_roots:
            p_root = Path(os.path.expanduser(root_dir))
            if not p_root.exists():
                continue

            for dirpath, dirnames, filenames in os.walk(p_root):
                # Exclude noisy, credential, sandbox, or cache directories
                for ign in [".git", "node_modules", "venv", ".venv", "__pycache__", ".gemini", "dist", "build", "sandboxes", "runtime"]:
                    if ign in dirnames:
                        dirnames.remove(ign)

                for fname in filenames:
                    f_lower = fname.lower()
                    if f_lower in STEERING_FILENAMES or f_lower.endswith(".instructions.md"):
                        full_path = os.path.join(dirpath, fname)
                        doc = self._parse_steering_file(Path(full_path))
                        if doc:
                            discovered[doc.id] = doc

        self._documents = discovered
        self._detect_conflicts()
        return list(self._documents.values())

    def _parse_steering_file(self, path: Path) -> Optional[SteeringDocument]:
        """Parse content, rules, and scope from a candidate steering file."""
        try:
            stat = path.stat()
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.debug("Could not read steering file %s: %s", path, e)
            return None

        # Calculate hash
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Determine scope
        p_str = str(path.resolve())
        fname = path.name.lower()
        if "global" in p_str or "~/.config/" in p_str:
            scope = SteeringScope.GLOBAL
        elif "agentic_os" in p_str and fname == "agents.md":
            scope = SteeringScope.ORGANIZATION
        elif "/docs/agents.md" in p_str:
            scope = SteeringScope.PROJECT
        elif "/.github/" in p_str or "/neocheck-backend/" in p_str:
            scope = SteeringScope.REPOSITORY
        else:
            scope = SteeringScope.DIRECTORY

        priority = SCOPE_PRECEDENCE[scope]

        # Extract project name from path
        parts = path.parts
        project_name = "general"
        for part in reversed(parts[:-1]):
            if part in ("Agentic_shared_memory", "Agentic_os", "Neo-check", "Support_ticket", "docs"):
                project_name = part
                break
            elif part == _workspace_name():
                project_name = _workspace_name()
                break

        # Extract rules: lines starting with '-', '*', or numbers that express directives (MUST, NEVER, DO NOT, ALWAYS, PREFER)
        rules = []
        summary_lines = []
        for line in content.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#") and len(summary_lines) < 3:
                summary_lines.append(s.lstrip("#").strip())
            elif re.search(r"\b(must|never|do not|always|prefer|require|cannot|forbidden)\b", s, re.IGNORECASE):
                cleaned_rule = re.sub(r"^[-*\d.)\s]+", "", s).strip()
                if len(cleaned_rule) > 10 and cleaned_rule not in rules:
                    rules.append(cleaned_rule)

        summary = " / ".join(summary_lines) if summary_lines else path.name
        doc_id = f"{project_name.lower()}_{path.stem.lower()}_{content_hash[:8]}"

        # Infer tags
        tags = [scope.value.lower(), project_name.lower()]
        for kw in ["azure", "docker", "security", "git", "safety", "refactor", "coding"]:
            if kw in content.lower():
                tags.append(kw)

        return SteeringDocument(
            id=doc_id,
            path=str(path.resolve()),
            scope=scope,
            project=project_name,
            priority=priority,
            tags=tags,
            summary=summary,
            content_hash=content_hash,
            modified_time=stat.st_mtime,
            enabled=True,
            trust_level="high" if project_name != "general" else "medium",
            rules=rules[:20],  # cap to top 20 significant rules
            raw_content=content
        )

    def _detect_conflicts(self) -> None:
        """Analyze extracted rules across documents to detect contradictory directives."""
        self._conflicts.clear()
        docs = list(self._documents.values())

        # Opposite keyword pairs indicating potential conflict
        conflict_pairs = [
            (r"\balways\b", r"\bnever\b"),
            (r"\bmust\b", r"\bdo not\b"),
            (r"\bprefer stdlib\b", r"\bprefer framework\b"),
            (r"\bcommit automatically\b", r"\bdo not commit\b"),
        ]

        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                doc_a = docs[i]
                doc_b = docs[j]

                for rule_a in doc_a.rules:
                    for rule_b in doc_b.rules:
                        # Check keyword contradictions on similar subjects
                        for pat_a, pat_b in conflict_pairs:
                            if ((re.search(pat_a, rule_a, re.IGNORECASE) and re.search(pat_b, rule_b, re.IGNORECASE)) or
                                (re.search(pat_b, rule_a, re.IGNORECASE) and re.search(pat_a, rule_b, re.IGNORECASE))):
                                # Determine resolution by priority
                                if doc_a.priority > doc_b.priority:
                                    res = f"{doc_a.path} (scope {doc_a.scope.value}) overrides {doc_b.path}"
                                elif doc_b.priority > doc_a.priority:
                                    res = f"{doc_b.path} (scope {doc_b.scope.value}) overrides {doc_a.path}"
                                else:
                                    res = "Direct conflict with equal priority; operator review required"

                                self._conflicts.append(SteeringConflict(
                                    source_a=doc_a.path,
                                    source_b=doc_b.path,
                                    conflicting_rule=f"A: {rule_a[:80]} <==> B: {rule_b[:80]}",
                                    scope_a=doc_a.scope.value,
                                    scope_b=doc_b.scope.value,
                                    priority_a=doc_a.priority,
                                    priority_b=doc_b.priority,
                                    resolution=res
                                ))

    def register(self, doc: SteeringDocument) -> None:
        """Register or update a steering document in memory."""
        self._documents[doc.id] = doc
        self._detect_conflicts()

    def list_documents(self) -> List[SteeringDocument]:
        """Return all registered steering documents sorted by priority descending."""
        docs = list(self._documents.values())
        docs.sort(key=lambda d: d.priority, reverse=True)
        return docs

    def get_document(self, doc_id: str) -> Optional[SteeringDocument]:
        """Get a specific steering document."""
        return self._documents.get(doc_id)

    def get_conflicts(self) -> List[SteeringConflict]:
        """Return all detected steering conflicts."""
        return list(self._conflicts)

    def find_relevant_steering(self, task: str, project: str = "") -> List[SteeringDocument]:
        """Find and rank steering documents relevant to a given task description."""
        terms = set(re.findall(r"\b[a-z0-9_-]{2,}\b", task.lower()))
        scored: List[Tuple[SteeringDocument, float]] = []

        for doc in self._documents.values():
            if not doc.enabled:
                continue

            score = float(doc.priority)  # baseline by scope priority

            # Project match bonus
            if project and doc.project.lower() == project.lower():
                score += 25.0

            # Tag match bonus
            for tag in doc.tags:
                if tag in terms:
                    score += 15.0

            # Content token matches
            content_tokens = set(re.findall(r"\b[a-z0-9_-]{2,}\b", doc.summary.lower()))
            matches = terms.intersection(content_tokens)
            score += len(matches) * 5.0

            scored.append((doc, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in scored]
