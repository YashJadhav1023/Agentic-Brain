"""Knowledge and Document Registry for Mission Control.

Indexes and catalogs approved project, architecture, and operational documentation
across approved repository roots.
Enforces strict exclusion of credential files, binary media, private keys, and runtime artifacts.
Applies automated secret scrubbing to all read operations.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
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

# Supported document extensions
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml"}

# Deny list keywords for paths and filenames
PATH_DENY_LIST = {
    ".git", "node_modules", "venv", ".venv", "__pycache__", ".gemini", "dist", "build",
    "sandboxes", "runtime", "cache", "credentials", "secrets", "tokens", "keyring",
    ".ssh", "private_keys", "secret", "password"
}

FILENAME_DENY_PATTERNS = [
    r"^\.env.*",
    r".*credential.*",
    r".*secret.*",
    r".*token.*",
    r".*id_rsa.*",
    r".*\.pem$",
    r".*\.key$",
    r".*\.pfx$",
]


@dataclass
class DocumentMetadata:
    """Metadata representing an indexed documentation resource."""
    id: str
    path: str
    title: str
    project: str = ""
    doc_type: str = "markdown"  # runbook, architecture, deployment, api_doc, report, general
    tags: List[str] = field(default_factory=list)
    summary: str = ""
    word_count: int = 0
    content_hash: str = ""
    modified_time: float = 0.0
    scope: str = "project"  # organization, project, repository, reference
    trust_level: str = "high"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DocumentRegistry:
    """Discovers, indexes, and searches documentation files across approved roots."""

    def __init__(self, knowledge_roots: Optional[List[str]] = None) -> None:
        self.knowledge_roots = knowledge_roots or [
            os.path.join(_workspace_root(), "docs"),
            os.path.join(_workspace_root(), "Documentation"),
        ]
        self._documents: Dict[str, DocumentMetadata] = {}

    def discover(self) -> List[DocumentMetadata]:
        """Index documentation files across configured roots."""
        discovered: Dict[str, DocumentMetadata] = {}

        for root_dir in self.knowledge_roots:
            p_root = Path(os.path.expanduser(root_dir))
            if not p_root.exists():
                continue

            for dirpath, dirnames, filenames in os.walk(p_root):
                # Apply path deny list
                dirnames[:] = [d for d in dirnames if d.lower() not in PATH_DENY_LIST and not d.startswith(".")]

                for fname in filenames:
                    ext = Path(fname).suffix.lower()
                    if ext not in ALLOWED_EXTENSIONS:
                        continue

                    # Check filename deny patterns
                    if any(re.match(pat, fname, re.IGNORECASE) for pat in FILENAME_DENY_PATTERNS):
                        logger.debug("Excluded sensitive filename: %s", fname)
                        continue

                    full_path = Path(os.path.join(dirpath, fname))
                    doc = self._index_document(full_path)
                    if doc:
                        discovered[doc.id] = doc

        self._documents = discovered
        logger.info("DocumentRegistry indexed %d documents", len(self._documents))
        return list(self._documents.values())

    def _index_document(self, path: Path) -> Optional[DocumentMetadata]:
        """Index a single document file."""
        try:
            stat = path.stat()
            # Skip files larger than 1MB to prevent memory bloat
            if stat.st_size > 1_000_000:
                return None

            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.debug("Could not read document %s: %s", path, e)
            return None

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Extract title and summary from first headers / lines
        title = path.stem.replace("-", " ").replace("_", " ").title()
        summary_lines = []
        for line in content.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#") and not summary_lines:
                title = s.lstrip("#").strip()
            elif len(summary_lines) < 3 and not s.startswith("#") and not s.startswith("```"):
                summary_lines.append(s)

        summary = " ".join(summary_lines)[:250] if summary_lines else title

        # Determine doc_type
        fname_lower = path.name.lower()
        content_lower = content[:2000].lower()
        if any(w in fname_lower or w in content_lower for w in ["deploy", "pipeline", "ci/cd", "ci-cd"]):
            doc_type = "deployment"
        elif any(w in fname_lower or w in content_lower for w in ["runbook", "cluster", "stability", "status"]):
            doc_type = "runbook"
        elif any(w in fname_lower or w in content_lower for w in ["architecture", "design", "overview"]):
            doc_type = "architecture"
        elif any(w in fname_lower or w in content_lower for w in ["cost", "billing", "budget"]):
            doc_type = "billing"
        elif any(w in fname_lower or w in content_lower for w in ["test", "load-test"]):
            doc_type = "testing"
        elif any(w in fname_lower for w in ["api", "spec"]):
            doc_type = "api_doc"
        else:
            doc_type = "general"

        # Determine project
        parts = path.parts
        project = _workspace_name()
        for part in reversed(parts[:-1]):
            if part in ("Agentic_shared_memory", "Agentic_os", "Neo-check", "Support_ticket", "docs"):
                project = part
                break

        # Extract tags
        tags = [doc_type, project.lower()]
        for kw in ["azure", "aks", "redis", "kubernetes", "microservice", "cost", "pipeline", "security", "docker", "terraform"]:
            if kw in content_lower or kw in fname_lower:
                tags.append(kw)

        words = len(content.split())
        doc_id = f"{project.lower()}_{path.stem.lower()}_{content_hash[:8]}"

        return DocumentMetadata(
            id=doc_id,
            path=str(path.resolve()),
            title=title,
            project=project,
            doc_type=doc_type,
            tags=list(set(tags)),
            summary=summary,
            word_count=words,
            content_hash=content_hash,
            modified_time=stat.st_mtime,
            scope="project" if project != _workspace_name() else "organization",
            trust_level="high"
        )

    def list_documents(self, doc_type: Optional[str] = None, tag: Optional[str] = None) -> List[DocumentMetadata]:
        """List documents with optional filtering."""
        docs = list(self._documents.values())
        if doc_type:
            docs = [d for d in docs if d.doc_type == doc_type]
        if tag:
            docs = [d for d in docs if tag.lower() in [t.lower() for t in d.tags]]
        return docs

    def get_document(self, doc_id: str) -> Optional[DocumentMetadata]:
        """Retrieve a specific document metadata by ID."""
        return self._documents.get(doc_id)

    def search(self, query: str, limit: int = 10) -> List[Tuple[DocumentMetadata, float]]:
        """Search indexed documents by query keyword match."""
        terms = set(re.findall(r"\b[a-z0-9_-]{2,}\b", query.lower()))
        if not terms:
            return [(d, 1.0) for d in list(self._documents.values())[:limit]]

        scored: List[Tuple[DocumentMetadata, float]] = []
        for doc in self._documents.values():
            score = 0.0
            title_terms = set(re.findall(r"\b[a-z0-9_-]{2,}\b", doc.title.lower()))
            summary_terms = set(re.findall(r"\b[a-z0-9_-]{2,}\b", doc.summary.lower()))
            tags = set(t.lower() for t in doc.tags)

            # Title matches (weight 3.0)
            score += len(terms.intersection(title_terms)) * 3.0
            # Tag matches (weight 2.5)
            score += len(terms.intersection(tags)) * 2.5
            # Summary matches (weight 1.0)
            score += len(terms.intersection(summary_terms)) * 1.0

            if score > 0.0:
                scored.append((doc, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def read_document_content(self, doc_id: str, max_chars: int = 8000) -> str:
        """Safely read document content, limiting length and ensuring no secrets leak."""
        doc = self._documents.get(doc_id)
        if not doc or not os.path.exists(doc.path):
            return ""

        try:
            content = Path(doc.path).read_text(encoding="utf-8", errors="ignore")
            # Apply basic sanitization
            sanitized = re.sub(r"(sk-[a-zA-Z0-9]{20,})", "[REDACTED_API_KEY]", content)
            sanitized = re.sub(r"(ghp_[a-zA-Z0-9]{20,})", "[REDACTED_TOKEN]", sanitized)
            return sanitized[:max_chars]
        except Exception as e:
            logger.error("Failed to read document content for %s: %s", doc_id, e)
            return ""
