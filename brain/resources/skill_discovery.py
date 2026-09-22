"""Skill Discovery Engine for Mission Control Phase 16.

Discovers AI agent skills from approved safe roots in a strictly READ-ONLY manner.
Never modifies external configuration or skill files.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.resources.resource_model import (
    PermissionLevel,
    Resource,
    ResourceLifecycleState,
    ResourceType,
    TrustLevel,
)

logger = logging.getLogger(__name__)


@dataclass
class DiscoveredSkill:
    """Represents a skill discovered on the system or repository."""
    id: str
    name: str
    location: str
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    source: str = "skills"
    parameters: Dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
    trust_level: TrustLevel = TrustLevel.HIGH
    lifecycle_state: ResourceLifecycleState = ResourceLifecycleState.ENABLED
    approval_required: bool = False
    category: str = ""
    provenance_hash: str = ""

    def to_resource(self) -> Resource:
        """Convert to normalized Resource representation."""
        res_id = self.id if self.id.startswith("skill:") else f"skill:{self.id}"
        return Resource(
            id=res_id,
            type=ResourceType.SKILL,
            name=self.name,
            location=self.location,
            description=self.description,
            capabilities=list(self.capabilities),
            source=self.source,
            trust_level=self.trust_level,
            permission_level=PermissionLevel.READ_ONLY,
            read_only=True,
            lifecycle_state=self.lifecycle_state,
            availability=(self.lifecycle_state == ResourceLifecycleState.ENABLED),
            health="healthy" if self.lifecycle_state != ResourceLifecycleState.DISABLED else "disabled",
            metadata={
                "parameters": self.parameters,
                "has_prompt": bool(self.prompt_template),
                "approval_required": self.approval_required,
                "category": self.category,
                "provenance_hash": self.provenance_hash,
            },
            tags=list(self.tags),
        )


class SkillDiscoveryEngine:
    """Discovers skills across approved, safe directory paths."""

    def __init__(self, search_roots: Optional[List[str]] = None) -> None:
        self.search_roots = search_roots or self._get_default_search_roots()

    def _get_default_search_roots(self) -> List[str]:
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(".", "skills"),
            os.path.join(home, ".gemini", "antigravity-cli", "builtin", "skills"),
            os.path.join(home, ".gemini", "config", "plugins"),
            os.path.join(os.environ.get("BRAIN_WORKSPACE_ROOT", os.getcwd()), ".agents", "skills"),
        ]
        return [c for c in candidates if os.path.exists(c)]

    def discover_ecc_skills(self) -> List[DiscoveredSkill]:
        """Discover Category A and B skills from ECC without modifying them."""
        try:
            from brain.resources.ecc_normalizer import ECCCategory, ECCComponentNormalizer
            norm = ECCComponentNormalizer()
            ecc_components = norm.discover_skills()
        except Exception as e:
            logger.debug("Failed discovering ECC skills: %s", e)
            return []

        discovered: List[DiscoveredSkill] = []
        for comp in ecc_components:
            if comp.category not in (ECCCategory.A, ECCCategory.B):
                continue
            skill_id = comp.id if comp.id.startswith("ecc:") else f"ecc:{comp.name.lower().replace(' ', '-')}"
            d_skill = DiscoveredSkill(
                id=skill_id,
                name=f"ECC {comp.name}",
                location=comp.location,
                description=comp.description,
                capabilities=list(comp.capabilities),
                tags=["ecc", "federated", comp.category.value.lower()] + list(comp.tags),
                source="ecc",
                trust_level=TrustLevel.EXTERNAL,
                lifecycle_state=ResourceLifecycleState.DISCOVERED,
                approval_required=comp.approval_required,
                category=comp.category.value,
                provenance_hash=comp.provenance_hash,
            )
            discovered.append(d_skill)
        return discovered

    def discover(self, include_ecc: bool = False) -> List[DiscoveredSkill]:
        """Scan approved directories for SKILL.md and skill specifications."""
        discovered: Dict[str, DiscoveredSkill] = {}

        for root_str in self.search_roots:
            root_path = Path(root_str)
            if not root_path.exists():
                continue

            # Look for SKILL.md files (at depth <= 4 to prevent deep recursive descent)
            try:
                for p in root_path.glob("**/SKILL.md"):
                    self._parse_skill_file(p, discovered)
            except Exception as e:
                logger.debug("Error traversing skills in %s: %s", root_str, e)

        if include_ecc:
            for ecc_s in self.discover_ecc_skills():
                bare_id = ecc_s.id.split(":", 1)[-1]
                if ecc_s.id not in discovered and bare_id not in discovered:
                    discovered[ecc_s.id] = ecc_s

        return list(discovered.values())

    def _parse_skill_file(self, path: Path, out: Dict[str, DiscoveredSkill]) -> None:
        """Parse a single SKILL.md file safely without modifying it."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(16384)  # cap reading at 16KB
        except Exception as e:
            logger.debug("Failed reading skill file %s: %s", path, e)
            return

        skill_dir = path.parent
        skill_id = skill_dir.name.lower().replace("_", "-")
        if not skill_id or skill_id == "skills":
            skill_id = path.parent.parent.name.lower().replace("_", "-")

        # Parse YAML frontmatter if present
        description = ""
        name = skill_id.replace("-", " ").title()
        caps: List[str] = [skill_id]

        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if fm_match:
            fm_text = fm_match.group(1)
            for fline in fm_text.splitlines():
                stripped = fline.strip()
                if stripped.startswith("name:"):
                    val = stripped.split(":", 1)[1].strip()
                    if (val.startswith("\"") and val.endswith("\"")) or (val.startswith("\x27") and val.endswith("\x27")):
                        val = val[1:-1].strip()
                    name = val
                elif stripped.startswith("description:"):
                    val = stripped.split(":", 1)[1].strip()
                    if (val.startswith("\"") and val.endswith("\"")) or (val.startswith("\x27") and val.endswith("\x27")):
                        val = val[1:-1].strip()
                    description = val
        else:
            # First heading
            h_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
            if h_match:
                name = h_match.group(1).strip()
            # First paragraph after heading
            p_match = re.search(r"^([^#\n][^\n]+)", content, re.MULTILINE)
            if p_match:
                description = p_match.group(1).strip()

        # Infer capabilities
        text_lower = f"{name} {description}".lower()
        for term in ["kubernetes", "azure", "docker", "flutter", "dart", "python", "chrome", "debug", "test", "database", "security"]:
            if term in text_lower and term not in caps:
                caps.append(term)

        out[skill_id] = DiscoveredSkill(
            id=skill_id,
            name=name,
            location=str(path),
            description=description,
            capabilities=caps,
            tags=["skill", skill_id],
            source=f"plugin:{skill_dir.parent.name}" if "plugins" in str(path) else "builtin",
            prompt_template="",
        )
