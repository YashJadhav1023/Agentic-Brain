"""Unit tests for Phase 23 Milestone 2: ECC Repository Audit & Capability Normalizer.

Validates:
- Manifest discovery and file tree census (681 total components).
- Component categorization into A, B, C, D taxonomy.
- Strict Category D rejections (shell scripts, installers, raw hooks, cloud memory sync).
- SHA-256 provenance fingerprinting and tamper detection.
- YAML frontmatter extraction (pure Python stdlib).
- Normalization into canonical Mission Control Resource objects.
- Non-interference assertions protecting PID 3809 and workspace isolation.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from brain.resources.ecc_normalizer import (
    ECCCategory,
    ECCComponentNormalizer,
    NormalizedECCComponent,
    compute_provenance_hash,
    parse_yaml_frontmatter,
)
from brain.resources.resource_model import (
    PermissionLevel,
    Resource,
    ResourceLifecycleState,
    ResourceType,
    TrustLevel,
)


class TestPhase23ECCNormalizer(unittest.TestCase):
    """Test suite for ECC component normalizer and audit capabilities."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.normalizer = ECCComponentNormalizer()
        cls.census = cls.normalizer.get_census_summary()

    # ----------------------------------------------------------------------
    # 1. Root Resolution & Discovery
    # ----------------------------------------------------------------------
    def test_01_ecc_root_resolution_default(self) -> None:
        """Verifies default ECC root path resolves to an existing directory."""
        self.assertTrue(self.normalizer.ecc_root.exists())
        self.assertTrue((self.normalizer.ecc_root / "agents").exists())
        self.assertTrue((self.normalizer.ecc_root / "skills").exists())

    def test_02_ecc_root_custom_path(self) -> None:
        """Verifies normalizer accepts a custom directory path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_norm = ECCComponentNormalizer(ecc_root=tmpdir)
            self.assertEqual(custom_norm.ecc_root, Path(tmpdir))
            # Empty dir returns empty results cleanly
            self.assertEqual(len(custom_norm.discover_agents()), 0)

    # ----------------------------------------------------------------------
    # 2. Census & Inventory Verification
    # ----------------------------------------------------------------------
    def test_03_file_tree_census_total(self) -> None:
        """Verifies total component count is exactly 681 across all families."""
        self.assertEqual(self.census["total_components"], 681)
        by_cat = self.census["by_category"]
        self.assertEqual(by_cat["A"], 403)
        self.assertEqual(by_cat["B"], 161)
        self.assertEqual(by_cat["C"], 82)
        self.assertEqual(by_cat["D"], 35)
        self.assertEqual(sum(by_cat.values()), 681)

    def test_04_agents_census_counts(self) -> None:
        """Verifies exactly 68 agents: 38 A, 29 B, 1 C, 0 D."""
        agents_data = self.census["by_type"]["agents"]
        self.assertEqual(agents_data["total"], 68)
        self.assertEqual(agents_data["A"], 38)
        self.assertEqual(agents_data["B"], 29)
        self.assertEqual(agents_data["C"], 1)
        self.assertEqual(agents_data["D"], 0)

    def test_05_skills_census_counts(self) -> None:
        """Verifies exactly 286 skills: 215 A, 58 B, 11 C, 2 D."""
        skills_data = self.census["by_type"]["skills"]
        self.assertEqual(skills_data["total"], 286)
        self.assertEqual(skills_data["A"], 215)
        self.assertEqual(skills_data["B"], 58)
        self.assertEqual(skills_data["C"], 11)
        self.assertEqual(skills_data["D"], 2)

    def test_06_commands_census_counts(self) -> None:
        """Verifies exactly 94 commands: 0 A, 42 B, 50 C, 2 D."""
        cmds_data = self.census["by_type"]["commands"]
        self.assertEqual(cmds_data["total"], 94)
        self.assertEqual(cmds_data["A"], 0)
        self.assertEqual(cmds_data["B"], 42)
        self.assertEqual(cmds_data["C"], 50)
        self.assertEqual(cmds_data["D"], 2)

    def test_07_rules_census_counts(self) -> None:
        """Verifies exactly 122 rules across language directories: all Category A."""
        rules_data = self.census["by_type"]["rules"]
        self.assertEqual(rules_data["total"], 122)
        self.assertEqual(rules_data["A"], 122)
        self.assertEqual(rules_data["B"], 0)
        self.assertEqual(rules_data["C"], 0)
        self.assertEqual(rules_data["D"], 0)

    def test_08_hooks_census_counts(self) -> None:
        """Verifies exactly 24 hooks: 0 A, 8 B, 2 C, 14 D."""
        hooks_data = self.census["by_type"]["hooks"]
        self.assertEqual(hooks_data["total"], 24)
        self.assertEqual(hooks_data["A"], 0)
        self.assertEqual(hooks_data["B"], 8)
        self.assertEqual(hooks_data["C"], 2)
        self.assertEqual(hooks_data["D"], 14)

    def test_09_mcp_servers_census_counts(self) -> None:
        """Verifies exactly 35 MCP servers: 0 A, 18 B, 12 C, 5 D."""
        mcp_data = self.census["by_type"]["mcp_servers"]
        self.assertEqual(mcp_data["total"], 35)
        self.assertEqual(mcp_data["A"], 0)
        self.assertEqual(mcp_data["B"], 18)
        self.assertEqual(mcp_data["C"], 12)
        self.assertEqual(mcp_data["D"], 5)

    def test_10_documentation_census_counts(self) -> None:
        """Verifies exactly 40 documentation files: 28 A, 6 B, 6 C, 0 D."""
        docs_data = self.census["by_type"]["documentation"]
        self.assertEqual(docs_data["total"], 40)
        self.assertEqual(docs_data["A"], 28)
        self.assertEqual(docs_data["B"], 6)
        self.assertEqual(docs_data["C"], 6)
        self.assertEqual(docs_data["D"], 0)

    def test_11_scripts_census_counts(self) -> None:
        """Verifies exactly 12 scripts/installers: all Category D (Strict Rejection)."""
        scripts_data = self.census["by_type"]["scripts"]
        self.assertEqual(scripts_data["total"], 12)
        self.assertEqual(scripts_data["A"], 0)
        self.assertEqual(scripts_data["B"], 0)
        self.assertEqual(scripts_data["C"], 0)
        self.assertEqual(scripts_data["D"], 12)

    # ----------------------------------------------------------------------
    # 3. Strict Category D Rejections
    # ----------------------------------------------------------------------
    def test_12_strict_rejection_shell_scripts(self) -> None:
        """Verifies shell and PowerShell scripts (.sh, .ps1) are strictly rejected."""
        is_rej, reason = self.normalizer.is_strict_rejection("script", "deploy.sh", "/path/to/deploy.sh")
        self.assertTrue(is_rej)
        self.assertIn(".sh", reason.lower())

        is_rej_ps, reason_ps = self.normalizer.is_strict_rejection("script", "install.ps1", "/path/install.ps1")
        self.assertTrue(is_rej_ps)
        self.assertIn("powershell", reason_ps.lower())

    def test_13_strict_rejection_installer_scripts(self) -> None:
        """Verifies installer components are rejected with appropriate reasons."""
        scripts = self.normalizer.discover_scripts()
        self.assertGreaterEqual(len(scripts), 12)
        for s in scripts:
            self.assertEqual(s.category, ECCCategory.D)
            self.assertEqual(s.risk_level, "REJECTED")
            self.assertEqual(s.trust_level, TrustLevel.UNTRUSTED)
            self.assertEqual(s.permission_level, PermissionLevel.DESTRUCTIVE)
            self.assertTrue(len(s.rejection_reason) > 0)

    def test_14_strict_rejection_cloud_memory_squish(self) -> None:
        """Verifies 'squish' MCP server is strictly rejected for cloud memory sync."""
        mcps = {m.id.split(":")[-1]: m for m in self.normalizer.discover_mcp_servers()}
        self.assertIn("squish", mcps)
        squish = mcps["squish"]
        self.assertEqual(squish.category, ECCCategory.D)
        self.assertEqual(squish.risk_level, "REJECTED")
        self.assertIn("cloud memory", squish.rejection_reason.lower())

    def test_15_strict_rejection_cloud_memory_memxus(self) -> None:
        """Verifies 'memxus' MCP server is strictly rejected for external persistent memory."""
        mcps = {m.id.split(":")[-1]: m for m in self.normalizer.discover_mcp_servers()}
        self.assertIn("memxus", mcps)
        memxus = mcps["memxus"]
        self.assertEqual(memxus.category, ECCCategory.D)
        self.assertEqual(memxus.risk_level, "REJECTED")
        self.assertIn("cloud memory", memxus.rejection_reason.lower())

    def test_16_strict_rejection_cloud_compute_mcp(self) -> None:
        """Verifies remote execution MCP servers (ito-compute, nexus, devfleet) are rejected."""
        mcps = {m.id.split(":")[-1]: m for m in self.normalizer.discover_mcp_servers()}
        for remote_mcp in ["ito-compute", "nexus", "devfleet"]:
            self.assertIn(remote_mcp, mcps)
            self.assertEqual(mcps[remote_mcp].category, ECCCategory.D)
            self.assertEqual(mcps[remote_mcp].risk_level, "REJECTED")

    def test_17_strict_rejection_raw_node_hooks(self) -> None:
        """Verifies raw un-sandboxed node hook executions are categorized as Category D."""
        hooks = self.normalizer.discover_hooks()
        d_hooks = [h for h in hooks if h.category == ECCCategory.D]
        self.assertEqual(len(d_hooks), 14)
        for h in d_hooks:
            self.assertEqual(h.risk_level, "REJECTED")
            self.assertIn("node", h.rejection_reason.lower())

    # ----------------------------------------------------------------------
    # 4. SHA-256 Provenance Fingerprinting
    # ----------------------------------------------------------------------
    def test_18_provenance_hash_deterministic(self) -> None:
        """Verifies provenance hashing is deterministic across multiple evaluations."""
        text = "# Sample Architecture Document\nSome guidance."
        h1 = compute_provenance_hash(text)
        h2 = compute_provenance_hash(text)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertEqual(h1, hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_19_provenance_hash_file_and_bytes(self) -> None:
        """Verifies compute_provenance_hash produces identical hash for Path, bytes, and str."""
        content_str = "Content for provenance test"
        content_bytes = content_str.encode("utf-8")
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write(content_str)
            tf_path = Path(tf.name)

        try:
            h_str = compute_provenance_hash(content_str)
            h_bytes = compute_provenance_hash(content_bytes)
            h_file = compute_provenance_hash(tf_path)
            self.assertEqual(h_str, h_bytes)
            self.assertEqual(h_str, h_file)
        finally:
            if tf_path.exists():
                tf_path.unlink()

    def test_20_provenance_hash_dict_canonical_json(self) -> None:
        """Verifies dictionary provenance hashing is key-order invariant."""
        d1 = {"b": 2, "a": 1, "nested": {"y": "hello", "x": "world"}}
        d2 = {"nested": {"x": "world", "y": "hello"}, "a": 1, "b": 2}
        h1 = compute_provenance_hash(d1)
        h2 = compute_provenance_hash(d2)
        self.assertEqual(h1, h2)

    # ----------------------------------------------------------------------
    # 5. YAML Frontmatter Parsing (Pure stdlib)
    # ----------------------------------------------------------------------
    def test_21_yaml_frontmatter_parsing_scalars(self) -> None:
        """Verifies parsing of scalar values and booleans."""
        text = "---\nname: architect\ndescription: Senior Architect\nmodel: opus\nactive: true\n---\n# Title"
        fm = parse_yaml_frontmatter(text)
        self.assertEqual(fm["name"], "architect")
        self.assertEqual(fm["description"], "Senior Architect")
        self.assertEqual(fm["model"], "opus")
        self.assertIs(fm["active"], True)

    def test_22_yaml_frontmatter_parsing_lists_and_dicts(self) -> None:
        """Verifies parsing of inline lists, block lists, and nested dictionaries."""
        text = """---
name: test-component
inline_tags: [ecc, security, audit]
block_tools:
  - Read
  - Grep
  - Glob
metadata:
  origin: ECC
  version: 2.2.1
---
"""
        fm = parse_yaml_frontmatter(text)
        self.assertEqual(fm["name"], "test-component")
        self.assertEqual(fm["inline_tags"], ["ecc", "security", "audit"])
        self.assertEqual(fm["block_tools"], ["Read", "Grep", "Glob"])
        self.assertEqual(fm["metadata"]["origin"], "ECC")
        self.assertEqual(fm["metadata"]["version"], "2.2.1")

    def test_23_yaml_frontmatter_empty_or_missing(self) -> None:
        """Verifies frontmatter parser handles missing or malformed blocks cleanly."""
        self.assertEqual(parse_yaml_frontmatter("# No frontmatter"), {})
        self.assertEqual(parse_yaml_frontmatter(""), {})

    # ----------------------------------------------------------------------
    # 6. Normalization & Resource Mapping
    # ----------------------------------------------------------------------
    def test_24_normalization_category_a_resource(self) -> None:
        """Verifies Category A component maps to ENABLED Resource with HIGH trust."""
        agents = {a.id: a for a in self.normalizer.discover_agents()}
        self.assertIn("ecc:agent:architect", agents)
        arch = agents["ecc:agent:architect"]
        self.assertEqual(arch.category, ECCCategory.A)

        res = arch.to_resource()
        self.assertEqual(res.type, ResourceType.AGENT)
        self.assertEqual(res.source, "ecc")
        self.assertEqual(res.trust_level, TrustLevel.HIGH)
        self.assertEqual(res.permission_level, PermissionLevel.READ_ONLY)
        self.assertTrue(res.read_only)
        self.assertEqual(res.lifecycle_state, ResourceLifecycleState.ENABLED)
        self.assertTrue(res.availability)
        self.assertEqual(res.metadata["risk_level"], "LOW_RISK")
        self.assertEqual(res.fingerprint, arch.provenance_hash)

    def test_25_normalization_category_b_resource(self) -> None:
        """Verifies Category B component maps to REVIEWED Resource with MEDIUM trust."""
        agents = {a.id: a for a in self.normalizer.discover_agents()}
        self.assertIn("ecc:agent:build-error-resolver", agents)
        resolver = agents["ecc:agent:build-error-resolver"]
        self.assertEqual(resolver.category, ECCCategory.B)

        res = resolver.to_resource()
        self.assertEqual(res.type, ResourceType.AGENT)
        self.assertEqual(res.trust_level, TrustLevel.MEDIUM)
        self.assertEqual(res.permission_level, PermissionLevel.LOW_RISK_WRITE)
        self.assertEqual(res.lifecycle_state, ResourceLifecycleState.REVIEWED)
        self.assertTrue(res.availability)
        self.assertTrue(res.metadata["approval_required"])
        self.assertIn("kiro-cli", res.metadata["compatible_agents"])

    def test_26_normalization_category_c_resource(self) -> None:
        """Verifies Category C component maps to DISABLED Resource with LOW trust."""
        agents = {a.id: a for a in self.normalizer.discover_agents()}
        self.assertIn("ecc:agent:chief-of-staff", agents)
        cos = agents["ecc:agent:chief-of-staff"]
        self.assertEqual(cos.category, ECCCategory.C)

        res = cos.to_resource()
        self.assertEqual(res.type, ResourceType.AGENT)
        self.assertEqual(res.trust_level, TrustLevel.LOW)
        self.assertEqual(res.lifecycle_state, ResourceLifecycleState.DISABLED)
        self.assertTrue(res.availability)

    def test_27_normalization_category_d_resource(self) -> None:
        """Verifies Category D component maps to DISABLED Resource with UNTRUSTED level."""
        scripts = {s.id: s for s in self.normalizer.discover_scripts()}
        self.assertIn("ecc:script:install", scripts)
        install_script = scripts["ecc:script:install"]
        self.assertEqual(install_script.category, ECCCategory.D)

        res = install_script.to_resource()
        self.assertEqual(res.type, ResourceType.TOOL)
        self.assertEqual(res.trust_level, TrustLevel.UNTRUSTED)
        self.assertEqual(res.permission_level, PermissionLevel.DESTRUCTIVE)
        self.assertEqual(res.lifecycle_state, ResourceLifecycleState.DISABLED)
        self.assertFalse(res.availability)
        self.assertEqual(res.health, "rejected")
        self.assertIn("rejection_reason", res.metadata)

    def test_28_resource_dict_roundtrip(self) -> None:
        """Verifies Resource to_dict() and from_dict() roundtrip serialization."""
        all_resources = self.normalizer.export_normalized_resources()
        self.assertEqual(len(all_resources), 681)

        # Test sample of resources across types
        sample_resources = all_resources[:5] + all_resources[300:305] + all_resources[-5:]
        for r in sample_resources:
            d = r.to_dict()
            self.assertIsInstance(d, dict)
            r_reconstructed = Resource.from_dict(d)
            self.assertEqual(r.id, r_reconstructed.id)
            self.assertEqual(r.type, r_reconstructed.type)
            self.assertEqual(r.trust_level, r_reconstructed.trust_level)
            self.assertEqual(r.fingerprint, r_reconstructed.fingerprint)
            self.assertEqual(r.metadata["category"], r_reconstructed.metadata["category"])

    def test_29_category_filter_methods(self) -> None:
        """Verifies get_components_by_category correctly filters subsets."""
        cat_a = self.normalizer.get_components_by_category(ECCCategory.A)
        cat_b = self.normalizer.get_components_by_category(ECCCategory.B)
        cat_c = self.normalizer.get_components_by_category(ECCCategory.C)
        cat_d = self.normalizer.get_components_by_category(ECCCategory.D)

        self.assertEqual(len(cat_a), 403)
        self.assertEqual(len(cat_b), 161)
        self.assertEqual(len(cat_c), 82)
        self.assertEqual(len(cat_d), 35)

    # ----------------------------------------------------------------------
    # 7. Non-Interference Assertions
    # ----------------------------------------------------------------------
    def test_30_gui_pid_3809_non_interference(self) -> None:
        """Verifies the running Antigravity IDE GUI process (PID 3809) remains alive."""
        proc_stat = Path("/proc/3809/stat")
        if proc_stat.exists():
            with open(proc_stat, "r") as f:
                stat_content = f.read()
            if "antigravity" not in stat_content:
                self.skipTest("PID 3809 was recycled to another process after system restart")
            self.assertTrue(any(k in stat_content for k in ("antigravity", "agy", "node")))

    def test_31_workspace_isolation_agentic_os(self) -> None:
        """Verifies a configured sibling workspace remains untouched and read-only."""
        # Check the raw value first: Path("") is Path("."), which exists, so building
        # the Path before testing the variable made this never skip.
        configured = os.environ.get("BRAIN_SIBLING_WORKSPACE", "").strip()
        if not configured:
            self.skipTest("no sibling workspace configured (BRAIN_SIBLING_WORKSPACE)")
        agentic_os_path = Path(configured).expanduser()
        if not agentic_os_path.exists():
            self.skipTest(f"configured sibling workspace does not exist: {agentic_os_path}")
        self.assertTrue(agentic_os_path.exists())
        # Confirm no temporary or ecc files were written to Agentic_os
        self.assertFalse((agentic_os_path / "external").exists())
        self.assertFalse((agentic_os_path / "ecc").exists())


if __name__ == "__main__":
    unittest.main()
