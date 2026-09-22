"""Git Repository Awareness and Metadata Discovery for Mission Control.

Safely discovers connected Git repositories across approved project roots.
Collects branch, status, and commit metadata while strictly filtering out any
embedded tokens or credentials in remote URLs.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class RepositoryMetadata:
    """Metadata representing a connected local Git repository."""
    name: str
    path: str
    branch: str = "main"
    remotes: List[str] = field(default_factory=list)  # Remote names only, e.g., ['origin']
    clean: bool = True
    modified_count: int = 0
    untracked_count: int = 0
    recent_commits: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RepositoryRegistry:
    """Discovers and inspects Git repositories in approved workspace roots."""

    def __init__(self, search_roots: Optional[List[str]] = None) -> None:
        self.search_roots = search_roots or [
            os.environ.get("BRAIN_REPO_ROOT") or os.getcwd(),
            os.path.expanduser("."),
        ]
        self._repos: Dict[str, RepositoryMetadata] = {}

    def discover(self) -> List[RepositoryMetadata]:
        """Scan approved roots for Git repositories."""
        discovered: Dict[str, RepositoryMetadata] = {}

        for root_dir in self.search_roots:
            p_root = Path(os.path.expanduser(root_dir))
            if not p_root.exists():
                continue

            # Check if root_dir itself is a git repository
            if (p_root / ".git").exists():
                meta = self._inspect_git_repo(p_root)
                if meta:
                    discovered[meta.name] = meta

            # Check immediate children of root_dir
            if p_root.is_dir():
                try:
                    for child in p_root.iterdir():
                        if child.is_dir() and (child / ".git").exists():
                            meta = self._inspect_git_repo(child)
                            if meta and meta.name not in discovered:
                                discovered[meta.name] = meta
                except Exception as e:
                    logger.debug("Error listing children in %s: %s", p_root, e)

        self._repos = discovered
        logger.info("RepositoryRegistry discovered %d repositories", len(self._repos))
        return list(self._repos.values())

    def _inspect_git_repo(self, path: Path) -> Optional[RepositoryMetadata]:
        """Collect non-sensitive git status and branch information."""
        try:
            # 1. Current branch
            res_branch = subprocess.run(
                ["git", "-C", str(path), "branch", "--show-current"],
                capture_output=True, text=True, timeout=2.0
            )
            branch = res_branch.stdout.strip() or "detached"

            # 2. Status counts (modified and untracked)
            res_status = subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain"],
                capture_output=True, text=True, timeout=2.0
            )
            lines = res_status.stdout.strip().splitlines()
            modified_count = 0
            untracked_count = 0
            for line in lines:
                if line.startswith("??"):
                    untracked_count += 1
                elif line.strip():
                    modified_count += 1
            clean = (len(lines) == 0)

            # 3. Remotes (Names ONLY, e.g. 'origin', NEVER URLs containing tokens)
            res_remotes = subprocess.run(
                ["git", "-C", str(path), "remote"],
                capture_output=True, text=True, timeout=2.0
            )
            remotes = [r.strip() for r in res_remotes.stdout.splitlines() if r.strip()]

            # 4. Recent commits (max 3 subjects)
            res_log = subprocess.run(
                ["git", "-C", str(path), "log", "-n", "3", "--pretty=format:%s"],
                capture_output=True, text=True, timeout=2.0
            )
            recent_commits = [c.strip() for c in res_log.stdout.splitlines() if c.strip()]

            name = path.resolve().name
            if not name:
                return None
            tags = [name.lower()]
            if "agent" in name.lower():
                tags.append("agentic")
            if "ticket" in name.lower():
                tags.append("support")

            return RepositoryMetadata(
                name=name,
                path=str(path.resolve()),
                branch=branch,
                remotes=remotes,
                clean=clean,
                modified_count=modified_count,
                untracked_count=untracked_count,
                recent_commits=recent_commits,
                tags=tags
            )
        except Exception as e:
            logger.debug("Could not inspect git repo at %s: %s", path, e)
            return None

    def get_repo(self, name: str) -> Optional[RepositoryMetadata]:
        """Get repository metadata by name."""
        return self._repos.get(name)

    def list_repositories(self) -> List[RepositoryMetadata]:
        """List all discovered repositories."""
        return list(self._repos.values())
