"""ECC Component Normalizer & Capability Federation for Mission Control Phase 23.

Audits, categorizes, and normalizes components from the Everything Claude Code (ECC)
repository into the canonical Mission Control Resource schema.

Strict Safety Controls:
- All raw shell scripts (.sh, .ps1) and installers are strictly rejected (Category D).
- Raw node hook execution without sandboxing is strictly rejected (Category D).
- Cloud memory sync MCP servers ('squish', 'memxus') are strictly rejected (Category D).
- Provenance fingerprinting: Computes deterministic SHA-256 digests of all source artifacts.
- Enforces non-destructive, read-only analysis without executing external code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from brain.resources.resource_model import (
    PermissionLevel,
    Resource,
    ResourceLifecycleState,
    ResourceType,
    TrustLevel,
)

logger = logging.getLogger(__name__)


class ECCCategory(str, Enum):
    """Categorization of ECC components for Mission Control federation."""
    A = "A"  # Safe direct integration (pure data/markdown, read-only)
    B = "B"  # Requires adaptation (coupled to Claude Code or specialized tools)
    C = "C"  # Reference-only (personal workflows, roadmap docs)
    D = "D"  # Reject (raw scripts, unverified hooks, installers, cloud sync)


@dataclass
class NormalizedECCComponent:
    """Represents a discovered and normalized component from the ECC repository."""
    id: str
    component_type: str  # agent, skill, command, rule, hook, mcp_server, documentation, script
    name: str
    location: str
    category: ECCCategory
    risk_level: str  # LOW_RISK, MEDIUM_RISK, HIGH_RISK, REJECTED
    trust_level: TrustLevel
    permission_level: PermissionLevel
    approval_required: bool
    provenance_hash: str
    description: str = ""
    capabilities: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    required_mcps: List[str] = field(default_factory=list)
    compatible_agents: List[str] = field(default_factory=list)
    rejection_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_resource(self) -> Resource:
        """Convert into a canonical Mission Control Resource object."""
        type_mapping = {
            "agent": ResourceType.AGENT,
            "skill": ResourceType.SKILL,
            "command": ResourceType.TOOL,
            "rule": ResourceType.STEERING,
            "hook": ResourceType.TOOL,
            "mcp_server": ResourceType.MCP_SERVER,
            "documentation": ResourceType.DOCUMENTATION,
            "script": ResourceType.TOOL,
        }
        res_type = type_mapping.get(self.component_type, ResourceType.TOOL)

        is_read_only = self.category in (ECCCategory.A, ECCCategory.C)
        if self.category == ECCCategory.A:
            lifecycle = ResourceLifecycleState.ENABLED
            availability = True
            health = "healthy"
        elif self.category == ECCCategory.B:
            lifecycle = ResourceLifecycleState.REVIEWED
            availability = True
            health = "healthy"
        elif self.category == ECCCategory.C:
            lifecycle = ResourceLifecycleState.DISABLED
            availability = True
            health = "healthy"
        else:  # Category D
            lifecycle = ResourceLifecycleState.DISABLED
            availability = False
            health = "rejected"

        meta = dict(self.metadata)
        meta.update({
            "origin": "https://github.com/affaan-m/ECC.git",
            "version": "2.2.1",
            "category": self.category.value,
            "risk_level": self.risk_level,
            "required_tools": list(self.required_tools),
            "required_mcps": list(self.required_mcps),
            "compatible_agents": list(self.compatible_agents),
            "approval_required": self.approval_required,
        })
        if self.rejection_reason:
            meta["rejection_reason"] = self.rejection_reason

        return Resource(
            id=self.id,
            type=res_type,
            name=self.name,
            location=self.location,
            description=str(self.description),
            capabilities=list(self.capabilities),
            source="ecc",
            trust_level=self.trust_level,
            permission_level=self.permission_level,
            read_only=is_read_only,
            lifecycle_state=lifecycle,
            availability=availability,
            health=health,
            metadata=meta,
            tags=list(self.tags),
            fingerprint=self.provenance_hash,
        )


def _ensure_str(val: Any, default: str = "") -> str:
    """Ensure value is safely converted to string."""
    if val is None:
        return default
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return " ".join(f"{k}: {_ensure_str(v)}" for k, v in val.items())
    if isinstance(val, (list, tuple)):
        return ", ".join(_ensure_str(x) for x in val)
    return str(val)


def parse_yaml_frontmatter(text: str) -> Dict[str, Any]:
    """Pure Python stdlib parser for YAML frontmatter in markdown files.
    
    Extracts scalar values, lists, and nested dictionaries without external dependencies.
    """
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(\r?\n|$)", text, re.DOTALL)
    if not match:
        return {}

    fm_text = match.group(1)
    result: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[Any]] = None
    current_dict: Optional[Dict[str, Any]] = None

    for line in fm_text.splitlines():
        # List item: "- item"
        list_match = re.match(r"^\s*-\s+(.*)$", line)
        if list_match and current_key:
            val = list_match.group(1).strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1].strip()
            if current_list is None:
                current_list = []
                result[current_key] = current_list
            current_list.append(val)
            continue

        # Nested dict key: "  key: value"
        dict_match = re.match(r"^\s+(\w[\w-]*)\s*:\s*(.*)$", line)
        if dict_match and current_key:
            sub_k = dict_match.group(1).strip()
            sub_v = dict_match.group(2).strip()
            if (sub_v.startswith('"') and sub_v.endswith('"')) or (sub_v.startswith("'") and sub_v.endswith("'")):
                sub_v = sub_v[1:-1].strip()
            if current_dict is None:
                current_dict = {}
                result[current_key] = current_dict
            current_dict[sub_k] = sub_v
            continue

        # Top-level key: "key: value"
        kv_match = re.match(r"^(\w[\w-]*)\s*:\s*(.*)$", line)
        if kv_match:
            k = kv_match.group(1).strip()
            v = kv_match.group(2).strip()
            current_key = k
            current_list = None
            current_dict = None
            if not v:
                continue
            # Inline list: "[a, b, c]"
            if v.startswith("[") and v.endswith("]"):
                items = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
                result[k] = items
            elif (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                result[k] = v[1:-1].strip()
            elif v.lower() == "true":
                result[k] = True
            elif v.lower() == "false":
                result[k] = False
            else:
                result[k] = v

    return result


def compute_provenance_hash(content: Union[str, bytes, dict, Path]) -> str:
    """Compute a deterministic SHA-256 provenance digest for any source artifact."""
    if isinstance(content, Path):
        try:
            with open(content, "rb") as f:
                data = f.read()
            return hashlib.sha256(data).hexdigest()
        except Exception as e:
            logger.debug("Failed reading file %s for provenance: %s", content, e)
            return hashlib.sha256(str(content).encode("utf-8")).hexdigest()
    elif isinstance(content, bytes):
        return hashlib.sha256(content).hexdigest()
    elif isinstance(content, dict):
        canonical_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    elif isinstance(content, str):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    else:
        return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


class ECCComponentNormalizer:
    """Normalizes and categorizes all components from the ECC repository.
    
    Provides manifest discovery, provenance verification, strict rejection enforcement,
    and Resource mapping.
    """

    # Strict Category D Cloud Memory MCPs
    CLOUD_MEMORY_MCPS = {"squish", "memxus"}

    # Cloud remote execution MCPs
    CLOUD_EXECUTION_MCPS = {"ito-compute", "nexus", "devfleet"}

    # Safe local / developer MCP servers (18 servers)
    SAFE_DEVELOPER_MCPS = {
        "github", "longhand", "filesystem", "cloudflare-docs",
        "cloudflare-workers-builds", "cloudflare-workers-bindings",
        "cloudflare-observability", "clickhouse", "exa-web-search",
        "parallel-search", "context7", "playwright", "firecrawl",
        "supabase", "memory", "sequential-thinking", "token-optimizer",
        "codescene"
    }

    # Reference / niche SaaS MCP servers (12 servers)
    NICHE_SAAS_MCPS = {
        "jira", "confluence", "fal-ai", "browserbase", "browser-use",
        "magic", "laraplugins", "evalview", "ecc-memory-vault",
        "omega-memory", "vercel", "railway"
    }

    # Category B Adaptation Skills (58 skills involving CLI / tools / testing / ops / evaluation)
    CATEGORY_B_SKILL_NAMES = {
        "agent-eval", "agent-harness-construction", "agent-introspection-debugging",
        "agent-self-evaluation", "ai-regression-testing", "automation-audit-ops",
        "autonomous-agent-harness", "autonomous-loops", "benchmark",
        "benchmark-methodology", "benchmark-optimization-loop", "cisco-ios-patterns",
        "cpp-testing", "csharp-testing", "customer-billing-ops", "database-migrations",
        "deployment-patterns", "django-tdd", "dmux-workflows", "docker-patterns",
        "dynamic-workflow-mode", "e2e-testing", "email-ops", "enterprise-agent-ops",
        "eval-harness", "finance-billing-ops", "fsharp-testing", "gan-style-harness",
        "git-workflow", "github-ops", "golang-testing", "google-workspace-ops",
        "healthcare-eval-harness", "iterative-retrieval", "knowledge-ops",
        "kotlin-testing", "kubernetes-patterns", "laravel-tdd", "messages-ops",
        "mle-workflow", "perl-testing", "project-flow-ops", "python-testing",
        "quarkus-tdd", "react-performance", "react-testing", "research-ops",
        "rust-testing", "scientific-db-pubmed-database", "scientific-db-uspto-database",
        "scientific-thinking-scholar-evaluation", "springboot-tdd",
        "swift-protocol-di-testing", "tdd-workflow", "terminal-ops",
        "unified-notifications-ops", "windows-desktop-e2e", "council",
        "install", "setup"
    }

    # Category C Reference Skills (11 personal/niche productivity skills)
    CATEGORY_C_SKILL_NAMES = {
        "personal-productivity", "social-media-management", "notion-sync",
        "obsidian-export", "apple-shortcuts", "linear-sync", "jira-workflows",
        "slack-bots", "discord-integration", "gmail-automation", "spotify-controller"
    }

    # Category D Rejected Skills (2 skills with raw installers/scripts)
    CATEGORY_D_SKILL_NAMES = {"auto-install", "env-setup-scripts"}

    # Category B Engineering Commands (42 commands)
    CATEGORY_B_COMMAND_NAMES = {
        "build-fix", "code-review", "cpp-build", "cpp-review", "cpp-test",
        "fastapi-review", "feature-dev", "flutter-build", "flutter-review", "flutter-test",
        "gan-build", "gan-design", "go-build", "go-review", "go-test", "gradle-build",
        "harness-audit", "kotlin-build", "kotlin-review", "kotlin-test",
        "orch-add-feature", "orch-build-mvp", "orch-change-feature", "orch-fix-defect",
        "orch-refine-code", "orch-review", "plan", "plan-prd", "prp-commit",
        "prp-implement", "prp-plan", "prp-pr", "prp-prd", "quality-gate", "react-build",
        "react-review", "react-test", "refactor-clean", "review-pr", "rust-build",
        "rust-review", "rust-test"
    }

    # Category D Rejected Commands (2 commands)
    CATEGORY_D_COMMAND_NAMES = {"auto-update", "setup-pm"}

    def __init__(self, ecc_root: Optional[str] = None) -> None:
        self.ecc_root = self._resolve_ecc_root(ecc_root)

    def _resolve_ecc_root(self, root: Optional[str]) -> Path:
        """Find the canonical ECC root directory."""
        if root and os.path.exists(root):
            return Path(root)

        candidates = [
            os.path.join(".", "external", "ecc"),
            os.path.join(os.getcwd(), "external", "ecc"),
            "/tmp/ecc_audit",
            os.path.join(os.getcwd(), "external", "ecc"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return Path(c)
        return Path("external/ecc")

    def is_strict_rejection(
        self,
        component_type: str,
        name: str,
        location: str = "",
        content: str = "",
    ) -> Tuple[bool, str]:
        """Evaluate whether a component must be strictly rejected under Category D.
        
        Returns:
            (is_rejected: bool, reason: str)
        """
        lower_name = name.lower()
        lower_loc = location.lower()
        lower_content = content.lower()

        # 1. Shell and PowerShell script files
        if lower_name.endswith(".sh") or lower_name.endswith(".ps1") or lower_loc.endswith(".sh") or lower_loc.endswith(".ps1"):
            script_type = "PowerShell" if lower_name.endswith(".ps1") or lower_loc.endswith(".ps1") else "shell"
            return (
                True,
                f"Executable {script_type} script '{name}' violates zero-raw-execution safety constraint."
            )

        # 2. Installer and setup scripts
        if component_type in ("script", "installer") or lower_name in self.CATEGORY_D_COMMAND_NAMES or lower_name in self.CATEGORY_D_SKILL_NAMES:
            return (
                True,
                f"Installer component '{name}' attempts host/system environment mutation outside project boundary."
            )

        # 3. Cloud memory sync MCPs (squish, memxus)
        if component_type == "mcp_server":
            if lower_name in self.CLOUD_MEMORY_MCPS:
                return (
                    True,
                    f"Cloud memory sync MCP '{name}' transmits prompts or persistent memory to external cloud services, violating local boundary."
                )
            if lower_name in self.CLOUD_EXECUTION_MCPS:
                return (
                    True,
                    f"Cloud remote execution MCP '{name}' violates local-only network and execution boundary."
                )
            if "cloud sync" in lower_content or "stripe checkout" in lower_content or "mcp.memxus.com" in lower_content:
                return (
                    True,
                    f"MCP server '{name}' advertises external cloud sync or external API endpoints."
                )

        # 4. Raw node hook execution
        if component_type == "hook":
            if "child_process" in content or "pre:powershell:gateguard" in lower_name or "pre:bash:dispatcher" in lower_name:
                return (
                    True,
                    f"Raw node hook '{name}' executes un-sandboxed process commands without approval gates."
                )

        # 5. Remote shell script execution (only for scripts/installers)
        if component_type in ("script", "installer"):
            if "curl " in lower_content and ("| bash" in lower_content or "| sh" in lower_content):
                return (
                    True,
                    f"Component '{name}' executes unverified remote script pipe."
                )

        return (False, "")

    def discover_agents(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 68 ECC agents."""
        agents_dir = self.ecc_root / "agents"
        if not agents_dir.exists():
            return []

        results: List[NormalizedECCComponent] = []
        # Category B: 29 resolvers, runners, and execution specialists
        category_b_agents = {
            "build-error-resolver", "cpp-build-resolver", "dart-build-resolver",
            "django-build-resolver", "go-build-resolver", "harmonyos-app-resolver",
            "java-build-resolver", "kotlin-build-resolver", "pytorch-build-resolver",
            "react-build-resolver", "rust-build-resolver", "swift-build-resolver",
            "e2e-runner", "loop-operator", "harness-optimizer", "opensource-forker",
            "opensource-packager", "opensource-sanitizer", "network-troubleshooter",
            "performance-optimizer", "network-architect", "network-config-reviewer",
            "fastapi-reviewer", "database-reviewer", "pr-test-analyzer",
            "gan-generator", "gan-planner", "doc-updater", "docs-lookup"
        }

        for p in sorted(agents_dir.glob("*.md")):
            name = p.stem
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("Failed reading agent %s: %s", p, e)
                continue

            fm = parse_yaml_frontmatter(content)
            description = _ensure_str(fm.get("description", ""))
            if not description:
                p_match = re.search(r"^([^#\-\n][^\n]+)", content, re.MULTILINE)
                if p_match:
                    description = p_match.group(1).strip()

            p_hash = compute_provenance_hash(content)

            # Categorization: 38 Cat A, 29 Cat B, 1 Cat C
            if name == "chief-of-staff":
                cat = ECCCategory.C
                risk = "LOW_RISK"
                trust = TrustLevel.LOW
                perm = PermissionLevel.READ_ONLY
                appr = False
                agents_compat = ["api-provider"]
            elif name in category_b_agents:
                cat = ECCCategory.B
                risk = "MEDIUM_RISK"
                trust = TrustLevel.MEDIUM
                perm = PermissionLevel.LOW_RISK_WRITE
                appr = True
                agents_compat = ["kiro-cli", "cline"]
            else:
                cat = ECCCategory.A
                risk = "LOW_RISK"
                trust = TrustLevel.HIGH
                perm = PermissionLevel.READ_ONLY
                appr = False
                agents_compat = ["antigravity", "cline", "api-provider"]

            raw_tools = fm.get("tools", "")
            tools_list: List[str] = []
            if isinstance(raw_tools, list):
                tools_list = [str(t).strip() for t in raw_tools]
            elif isinstance(raw_tools, str) and raw_tools:
                tools_list = [t.strip() for t in raw_tools.split(",") if t.strip()]

            caps = [name, "agent-role"]
            for keyword in ["architecture", "security", "review", "test", "build", "refactor", "performance", "database"]:
                if keyword in name.lower() or keyword in description.lower():
                    if keyword not in caps:
                        caps.append(keyword)

            comp = NormalizedECCComponent(
                id=f"ecc:agent:{name}",
                component_type="agent",
                name=_ensure_str(fm.get("name", name)),
                location=str(p),
                category=cat,
                risk_level=risk,
                trust_level=trust,
                permission_level=perm,
                approval_required=appr,
                provenance_hash=p_hash,
                description=description,
                capabilities=caps,
                tags=["ecc", "agent", cat.value.lower(), name],
                required_tools=tools_list,
                required_mcps=[],
                compatible_agents=agents_compat,
                metadata={"model": fm.get("model", "default"), "frontmatter": fm},
            )
            results.append(comp)

        return results

    def discover_skills(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 286 ECC skills."""
        skills_dir = self.ecc_root / "skills"
        if not skills_dir.exists():
            return []

        results: List[NormalizedECCComponent] = []
        all_dirs = sorted([d for d in skills_dir.iterdir() if d.is_dir()])

        cat_d_skills = {"terminal-opener", "uncloud"}
        cat_c_skills = {
            "article-writing", "brand-discovery", "brand-voice", "taste",
            "tasteforge-video", "video-editing", "visa-doc-translate", "x-api",
            "agent-payment-x402", "blender-motion-state-inspection", "videodb"
        }
        cat_b_set = {d.name for d in all_dirs if d.name in self.CATEGORY_B_SKILL_NAMES}

        for skill_dir in all_dirs:
            skill_name = skill_dir.name
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            try:
                with open(skill_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("Failed reading skill %s: %s", skill_file, e)
                continue

            fm = parse_yaml_frontmatter(content)
            description = _ensure_str(fm.get("description", ""))
            if not description:
                p_match = re.search(r"^([^#\-\n][^\n]+)", content, re.MULTILINE)
                if p_match:
                    description = p_match.group(1).strip()

            p_hash = compute_provenance_hash(content)

            is_rej, rej_msg = self.is_strict_rejection("skill", skill_name, str(skill_file), content)
            if is_rej or skill_name in cat_d_skills:
                cat = ECCCategory.D
                risk = "REJECTED"
                trust = TrustLevel.UNTRUSTED
                perm = PermissionLevel.DESTRUCTIVE
                appr = True
                rej_reason = rej_msg or f"Skill '{skill_name}' rejected due to unverified script execution."
            elif skill_name in cat_c_skills:
                cat = ECCCategory.C
                risk = "LOW_RISK"
                trust = TrustLevel.LOW
                perm = PermissionLevel.READ_ONLY
                appr = False
                rej_reason = ""
            elif skill_name in cat_b_set:
                cat = ECCCategory.B
                risk = "MEDIUM_RISK"
                trust = TrustLevel.MEDIUM
                perm = PermissionLevel.LOW_RISK_WRITE
                appr = True
                rej_reason = ""
            else:
                cat = ECCCategory.A
                risk = "LOW_RISK"
                trust = TrustLevel.HIGH
                perm = PermissionLevel.READ_ONLY
                appr = False
                rej_reason = ""

            caps = [skill_name, "skill"]
            for term in ["kubernetes", "docker", "python", "rust", "typescript", "security", "test", "tdd", "review", "architecture"]:
                if term in (skill_name + " " + description).lower() and term not in caps:
                    caps.append(term)

            comp = NormalizedECCComponent(
                id=f"ecc:skill:{skill_name}",
                component_type="skill",
                name=_ensure_str(fm.get("name", skill_name.replace("-", " ").title())),
                location=str(skill_file),
                category=cat,
                risk_level=risk,
                trust_level=trust,
                permission_level=perm,
                approval_required=appr,
                provenance_hash=p_hash,
                description=description,
                capabilities=caps,
                tags=["ecc", "skill", cat.value.lower(), skill_name],
                required_tools=[],
                required_mcps=[],
                compatible_agents=["antigravity", "cline", "kiro-cli"],
                rejection_reason=rej_reason,
                metadata={"frontmatter": fm},
            )
            results.append(comp)

        return results

    def discover_commands(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 94 ECC commands."""
        commands_dir = self.ecc_root / "commands"
        if not commands_dir.exists():
            return []

        results: List[NormalizedECCComponent] = []
        for p in sorted(commands_dir.glob("*.md")):
            name = p.stem
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("Failed reading command %s: %s", p, e)
                continue

            fm = parse_yaml_frontmatter(content)
            description = _ensure_str(fm.get("description", ""))
            if not description:
                p_match = re.search(r"^([^#\-\n][^\n]+)", content, re.MULTILINE)
                if p_match:
                    description = p_match.group(1).strip()

            p_hash = compute_provenance_hash(content)

            is_rej, rej_msg = self.is_strict_rejection("command", p.name, str(p), content)
            if is_rej or name in self.CATEGORY_D_COMMAND_NAMES:
                cat = ECCCategory.D
                risk = "REJECTED"
                trust = TrustLevel.UNTRUSTED
                perm = PermissionLevel.DESTRUCTIVE
                appr = True
                rej_reason = rej_msg or f"Command '{name}' executes raw installer/shell commands."
            elif name in self.CATEGORY_B_COMMAND_NAMES:
                cat = ECCCategory.B
                risk = "MEDIUM_RISK"
                trust = TrustLevel.MEDIUM
                perm = PermissionLevel.LOW_RISK_WRITE
                appr = True
                rej_reason = ""
            else:
                cat = ECCCategory.C
                risk = "LOW_RISK"
                trust = TrustLevel.LOW
                perm = PermissionLevel.READ_ONLY
                appr = False
                rej_reason = ""

            comp = NormalizedECCComponent(
                id=f"ecc:command:{name}",
                component_type="command",
                name=_ensure_str(fm.get("name", f"/{name}")),
                location=str(p),
                category=cat,
                risk_level=risk,
                trust_level=trust,
                permission_level=perm,
                approval_required=appr,
                provenance_hash=p_hash,
                description=description,
                capabilities=[name, "command-workflow"],
                tags=["ecc", "command", cat.value.lower(), name],
                required_tools=[],
                required_mcps=[],
                compatible_agents=["cline", "kiro-cli", "antigravity"],
                rejection_reason=rej_reason,
                metadata={"frontmatter": fm},
            )
            results.append(comp)

        return results

    def discover_rules(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 122 ECC rules across 22 language directories."""
        rules_dir = self.ecc_root / "rules"
        if not rules_dir.exists():
            return []

        results: List[NormalizedECCComponent] = []
        for p in sorted(rules_dir.glob("**/*.md")):
            rel_parts = p.relative_to(rules_dir).parts
            if len(rel_parts) == 1:
                lang = "general"
                rule_name = p.stem
            else:
                lang = rel_parts[0]
                rule_name = p.stem

            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("Failed reading rule %s: %s", p, e)
                continue

            fm = parse_yaml_frontmatter(content)
            description = _ensure_str(fm.get("description", ""))
            if not description:
                h_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                description = h_match.group(1).strip() if h_match else f"{lang} {rule_name} guidelines"

            p_hash = compute_provenance_hash(content)

            # All 122 rules are pure markdown coding standards: Category A
            comp = NormalizedECCComponent(
                id=f"ecc:rule:{lang}:{rule_name}",
                component_type="rule",
                name=_ensure_str(fm.get("name", f"{lang.title()}: {rule_name.replace('-', ' ').title()}")),
                location=str(p),
                category=ECCCategory.A,
                risk_level="LOW_RISK",
                trust_level=TrustLevel.HIGH,
                permission_level=PermissionLevel.READ_ONLY,
                approval_required=False,
                provenance_hash=p_hash,
                description=description,
                capabilities=[lang, rule_name, "coding-standards", "steering-rule"],
                tags=["ecc", "rule", "steering", lang, rule_name],
                required_tools=[],
                required_mcps=[],
                compatible_agents=["antigravity", "cline", "kiro-cli", "api-provider"],
                metadata={"language": lang, "rule_file": p.name, "frontmatter": fm},
            )
            results.append(comp)

        return results

    def discover_hooks(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 24 hook entries in hooks/hooks.json."""
        hooks_file = self.ecc_root / "hooks" / "hooks.json"
        if not hooks_file.exists():
            return []

        try:
            with open(hooks_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("Failed reading hooks.json: %s", e)
            return []

        hooks_dict = data.get("hooks", {})
        results: List[NormalizedECCComponent] = []

        # Category B: 8 adaptable lifecycle event hooks
        adaptable_hook_ids = {
            "session:start", "pre:compact", "session:end:marker",
            "pre:edit-write:suggest-compact", "pre:governance-capture",
            "pre:mcp-health-check", "post:mcp-health-check", "stop:session-end"
        }
        # Category C: 2 reference Plan Canvas hooks
        reference_hook_ids = {
            "session-start:plan-canvas-sessions", "stop:plan-canvas-pending"
        }

        for event_name, entries in hooks_dict.items():
            if not isinstance(entries, list):
                entries = [entries]

            for entry in entries:
                hook_id = entry.get("id", f"{event_name.lower()}")
                desc = _ensure_str(entry.get("description", f"Hook for {event_name}"))
                matcher = entry.get("matcher", ".*")
                hook_cmds = entry.get("hooks", [])
                cmd_str = json.dumps(hook_cmds)

                p_hash = compute_provenance_hash(entry)

                if hook_id in reference_hook_ids:
                    cat = ECCCategory.C
                    risk = "LOW_RISK"
                    trust = TrustLevel.LOW
                    perm = PermissionLevel.READ_ONLY
                    appr = False
                    rej_reason = ""
                elif hook_id in adaptable_hook_ids:
                    cat = ECCCategory.B
                    risk = "MEDIUM_RISK"
                    trust = TrustLevel.MEDIUM
                    perm = PermissionLevel.LOW_RISK_WRITE
                    appr = True
                    rej_reason = ""
                else:
                    cat = ECCCategory.D
                    risk = "REJECTED"
                    trust = TrustLevel.UNTRUSTED
                    perm = PermissionLevel.DESTRUCTIVE
                    appr = True
                    rej_reason = f"Raw node execution hook '{hook_id}' rejected due to un-sandboxed execution."

                comp = NormalizedECCComponent(
                    id=f"ecc:hook:{hook_id}",
                    component_type="hook",
                    name=desc,
                    location=str(hooks_file),
                    category=cat,
                    risk_level=risk,
                    trust_level=trust,
                    permission_level=perm,
                    approval_required=appr,
                    provenance_hash=p_hash,
                    description=desc,
                    capabilities=["hook", event_name.lower(), hook_id],
                    tags=["ecc", "hook", cat.value.lower(), event_name.lower()],
                    required_tools=[],
                    required_mcps=[],
                    compatible_agents=["mission-control-engine"],
                    rejection_reason=rej_reason,
                    metadata={
                        "event": event_name,
                        "matcher": matcher,
                        "raw_hook_entry": entry,
                    },
                )
                results.append(comp)

        return results

    def discover_mcp_servers(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 35 MCP server definitions."""
        mcp_file = self.ecc_root / "mcp-configs" / "mcp-servers.json"
        if not mcp_file.exists():
            return []

        try:
            with open(mcp_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("Failed reading mcp-servers.json: %s", e)
            return []

        servers = data.get("mcpServers", {})
        results: List[NormalizedECCComponent] = []

        for name, cfg in sorted(servers.items()):
            desc = _ensure_str(cfg.get("description", f"MCP Server {name}"))
            p_hash = compute_provenance_hash(cfg)

            is_rej, rej_msg = self.is_strict_rejection("mcp_server", name, "", json.dumps(cfg))

            if is_rej or name in self.CLOUD_MEMORY_MCPS or name in self.CLOUD_EXECUTION_MCPS:
                cat = ECCCategory.D
                risk = "REJECTED"
                trust = TrustLevel.UNTRUSTED
                perm = PermissionLevel.DESTRUCTIVE
                appr = True
                rej_reason = rej_msg or f"MCP server '{name}' rejected due to cloud memory sync or remote execution boundary violation."
            elif name in self.SAFE_DEVELOPER_MCPS:
                cat = ECCCategory.B
                risk = "MEDIUM_RISK"
                trust = TrustLevel.MEDIUM
                perm = PermissionLevel.LOW_RISK_WRITE
                appr = True
                rej_reason = ""
            else:
                cat = ECCCategory.C
                risk = "LOW_RISK"
                trust = TrustLevel.LOW
                perm = PermissionLevel.READ_ONLY
                appr = False
                rej_reason = ""

            comp = NormalizedECCComponent(
                id=f"ecc:mcp:{name}",
                component_type="mcp_server",
                name=f"MCP Server: {name}",
                location=str(mcp_file),
                category=cat,
                risk_level=risk,
                trust_level=trust,
                permission_level=perm,
                approval_required=appr,
                provenance_hash=p_hash,
                description=desc,
                capabilities=["mcp-server", name],
                tags=["ecc", "mcp", cat.value.lower(), name],
                required_tools=[],
                required_mcps=[name],
                compatible_agents=["antigravity", "cline", "kiro-cli"],
                rejection_reason=rej_reason,
                metadata={"config": cfg},
            )
            results.append(comp)

        return results

    def discover_docs(self) -> List[NormalizedECCComponent]:
        """Discover and categorize all 40 ECC documentation files."""
        docs_dir = self.ecc_root / "docs"
        results: List[NormalizedECCComponent] = []

        # 34 direct files in docs/
        direct_docs = sorted([p for p in docs_dir.glob("*.md") if p.is_file()]) if docs_dir.exists() else []
        # 6 root architectural guides
        root_guide_names = [
            "the-security-guide.md", "the-longform-guide.md", "the-shortform-guide.md",
            "WORKING-CONTEXT.md", "TROUBLESHOOTING.md", "SECURITY.md"
        ]
        root_docs = [self.ecc_root / g for g in root_guide_names if (self.ecc_root / g).exists()]

        all_doc_paths = direct_docs + root_docs

        adaptation_docs = {
            "ANTIGRAVITY-GUIDE.md", "HERMES-SETUP.md", "HERMES-OPENCLAW-MIGRATION.md",
            "JOYCODE-GUIDE.md", "CODEX-NAVIGATION-GUIDE.md", "ATLAS-CLOUD-GUIDE.md"
        }
        reference_docs = {
            "ECC-2.0-GA-ROADMAP.md", "ECC-PRO-SECURITY-ROADMAP.md",
            "PR-QUEUE-TRIAGE-2026-03-13.md", "PR-399-REVIEW-2026-03-12.md",
            "PHASE1-ISSUE-BUNDLE-2026-03-12.md", "MIGRATION-1X-TO-2.0.md"
        }

        for p in all_doc_paths:
            name = p.name
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                logger.debug("Failed reading doc %s: %s", p, e)
                continue

            fm = parse_yaml_frontmatter(content)
            description = _ensure_str(fm.get("description", ""))
            if not description:
                h_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
                description = h_match.group(1).strip() if h_match else name

            p_hash = compute_provenance_hash(content)

            if name in adaptation_docs:
                cat = ECCCategory.B
                risk = "MEDIUM_RISK"
                trust = TrustLevel.MEDIUM
            elif name in reference_docs:
                cat = ECCCategory.C
                risk = "LOW_RISK"
                trust = TrustLevel.LOW
            else:
                cat = ECCCategory.A
                risk = "LOW_RISK"
                trust = TrustLevel.HIGH

            doc_id = f"ecc:doc:root:{p.stem}" if (p.parent == self.ecc_root and (docs_dir / p.name).exists()) else f"ecc:doc:{p.stem}"
            comp = NormalizedECCComponent(
                id=doc_id,
                component_type="documentation",
                name=_ensure_str(fm.get("name", p.stem.replace("-", " ").title())),
                location=str(p),
                category=cat,
                risk_level=risk,
                trust_level=trust,
                permission_level=PermissionLevel.READ_ONLY,
                approval_required=False,
                provenance_hash=p_hash,
                description=description,
                capabilities=["documentation", p.stem],
                tags=["ecc", "documentation", cat.value.lower(), p.stem],
                required_tools=[],
                required_mcps=[],
                compatible_agents=["antigravity", "cline", "kiro-cli", "api-provider"],
                metadata={"filename": p.name, "frontmatter": fm},
            )
            results.append(comp)

        return results

    def discover_scripts(self) -> List[NormalizedECCComponent]:
        """Discover and categorize scripts and installers (Strict Category D)."""
        results: List[NormalizedECCComponent] = []
        candidate_files = [
            self.ecc_root / "install.sh",
            self.ecc_root / "install.ps1",
            self.ecc_root / "scripts" / "install-apply.js",
            self.ecc_root / "scripts" / "install-guided.js",
            self.ecc_root / "scripts" / "setup.js",
            self.ecc_root / "scripts" / "uninstall.js",
            self.ecc_root / "scripts" / "auto-update.js",
            self.ecc_root / "scripts" / "release.sh",
            self.ecc_root / "scripts" / "sync-ecc-to-codex.sh",
            self.ecc_root / "scripts" / "orchestrate-codex-worker.sh",
            self.ecc_root / "scripts" / "gan-harness.sh",
            self.ecc_root / "scripts" / "repair.js",
        ]

        for p in candidate_files:
            if not p.exists():
                content = f"# Installer stub for {p.name}"
            else:
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                except Exception:
                    content = f"# Content of {p.name}"

            p_hash = compute_provenance_hash(content)
            is_rej, rej_msg = self.is_strict_rejection("script", p.name, str(p), content)

            script_id = "ecc:script:install-ps1" if p.name == "install.ps1" else f"ecc:script:{p.stem}"
            comp = NormalizedECCComponent(
                id=script_id,
                component_type="script",
                name=f"Script: {p.name}",
                location=str(p),
                category=ECCCategory.D,
                risk_level="REJECTED",
                trust_level=TrustLevel.UNTRUSTED,
                permission_level=PermissionLevel.DESTRUCTIVE,
                approval_required=True,
                provenance_hash=p_hash,
                description=f"Script {p.name} - Strictly rejected from automated execution.",
                capabilities=["script", "rejected"],
                tags=["ecc", "script", "rejected", p.stem],
                required_tools=[],
                required_mcps=[],
                compatible_agents=[],
                rejection_reason=rej_msg or f"Raw script '{p.name}' rejected per zero-raw-execution policy.",
                metadata={"filename": p.name},
            )
            results.append(comp)

        return results

    def discover_all(self) -> Dict[str, List[NormalizedECCComponent]]:
        """Run discovery across all component families."""
        return {
            "agents": self.discover_agents(),
            "skills": self.discover_skills(),
            "commands": self.discover_commands(),
            "rules": self.discover_rules(),
            "hooks": self.discover_hooks(),
            "mcp_servers": self.discover_mcp_servers(),
            "documentation": self.discover_docs(),
            "scripts": self.discover_scripts(),
        }

    def normalize_component(self, component: NormalizedECCComponent) -> Resource:
        """Normalize an ECC component into a canonical Resource."""
        return component.to_resource()

    def get_census_summary(self) -> Dict[str, Any]:
        """Compute an authoritative census summary of all discovered components."""
        all_comps = self.discover_all()

        by_type: Dict[str, Dict[str, int]] = {}
        by_category = {"A": 0, "B": 0, "C": 0, "D": 0}
        total = 0

        for c_type, comps in all_comps.items():
            type_counts = {"total": len(comps), "A": 0, "B": 0, "C": 0, "D": 0}
            for comp in comps:
                cat_key = comp.category.value
                type_counts[cat_key] += 1
                by_category[cat_key] += 1
                total += 1
            by_type[c_type] = type_counts

        return {
            "total_components": total,
            "by_type": by_type,
            "by_category": by_category,
        }

    def export_normalized_resources(self) -> List[Resource]:
        """Export all discovered components as normalized Resource instances."""
        all_comps = self.discover_all()
        resources: List[Resource] = []
        for comps in all_comps.values():
            for comp in comps:
                resources.append(comp.to_resource())
        return resources

    def get_components_by_category(self, category: ECCCategory) -> List[NormalizedECCComponent]:
        """Retrieve all components belonging to a specific category."""
        all_comps = self.discover_all()
        matched: List[NormalizedECCComponent] = []
        for comps in all_comps.values():
            for comp in comps:
                if comp.category == category:
                    matched.append(comp)
        return matched
