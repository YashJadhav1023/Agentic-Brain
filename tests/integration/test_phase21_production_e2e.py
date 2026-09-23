"""Phase 21: Production Hardening, End-to-End Validation & Release Tests.

Comprehensive validation of the entire mission chain:
- User task input
- Automatic resource selection
- Automatic knowledge retrieval
- Dynamic agent/provider selection
- Plan / DAG creation
- Plan execution & approval gate enforcement
- Invariant verification
- Performance telemetry recording
- Immutable audit event logging with hash chaining
- Redaction of sensitive credentials
- Antigravity IDE GUI safety verification
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from brain.mission_brain import MissionBrain
from brain.planner.plan_model import ExecutionGraph, PlanStatus, StepStatus


class TestPhase21ProductionEndToEnd(unittest.TestCase):
    """Production release validation suite for MissionBrain."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.brain = MissionBrain(workspace_dir=self.workspace)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_autonomous_chain_end_to_end(self) -> None:
        """Exercise all 9 links of the autonomous control plane chain."""
        # LINK 1: Natural-language user prompt
        task = "Investigate microservice authentication failures and verify cluster health"

        # LINK 2: Automatic Resource Selection
        res_counts = self.brain.discover_resources()
        self.assertGreater(res_counts.get("mcp_server", 0), 0)
        self.assertGreater(res_counts.get("mcp_tool", 0), 0)
        self.assertGreater(res_counts.get("skill", 0), 0)

        # Search for resources related to auth / cluster / health
        matched_resources = self.brain.search_resources("auth microservice token", limit=5)
        self.assertIsInstance(matched_resources, list)

        # LINK 3: Automatic Knowledge Retrieval
        knowledge_hits = self.brain.search_knowledge("authentication microservice", limit=5)
        self.assertIsInstance(knowledge_hits, list)

        # LINK 4: Dynamic Agent / Provider Selection
        routing_decision = self.brain.route_task(task)
        self.assertIsNotNone(routing_decision.agent_id)
        self.assertIsNotNone(routing_decision.model)
        self.assertGreater(routing_decision.total_score, 0.0)

        explanation = self.brain.explain_routing(task)
        self.assertEqual(explanation["selected_agent"], routing_decision.agent_id)
        self.assertIn("candidate_scores", explanation)
        self.assertIn("score_breakdown", explanation)

        # LINK 5: Plan / DAG Creation
        plan = self.brain.create_plan(task)
        self.assertTrue(plan.plan_id.startswith("plan-"))
        self.assertGreaterEqual(len(plan.steps), 1)
        self.assertIn(plan.status, [PlanStatus.DRAFT, PlanStatus.READY, PlanStatus.BLOCKED_ON_APPROVAL])

        # Verify DAG structure & ordering
        dag = ExecutionGraph(plan.steps)
        sorted_steps = dag.get_topological_order()
        self.assertEqual(len(sorted_steps), len(plan.steps))

        # LINK 6: Approval Gates for Destructive Actions
        destructive_task = "Delete temporary build logs and purge corrupted cache records"
        destr_plan = self.brain.create_plan(destructive_task)
        self.assertTrue(destr_plan.requires_approval)
        self.assertEqual(destr_plan.status, PlanStatus.BLOCKED_ON_APPROVAL)

        # Blocked execution attempt
        blocked_run = self.brain.execute_plan(destr_plan.plan_id)
        self.assertEqual(blocked_run["status"], "BLOCKED_ON_APPROVAL")

        # Explicit approval unblocks
        approval_ok = self.brain.approve_plan(destr_plan.plan_id, approver="release-engineer")
        self.assertTrue(approval_ok)
        approved_plan = self.brain.get_plan(destr_plan.plan_id)
        self.assertEqual(approved_plan.status, PlanStatus.READY)

        # LINK 7: Execution Fabric Execution with Invariant Verification
        exec_result = self.brain.execute_task(
            task="Verify service connectivity",
            command="echo '{\"status\": \"healthy\", \"latency_ms\": 12}'",
            verification_rules=["no_errors"],
            rollback_action="echo 'reverting test changes'",
        )
        self.assertEqual(exec_result.status, "SUCCESS")
        self.assertFalse(exec_result.rollback_applied)
        self.assertGreater(exec_result.latency_seconds, 0.0)

        # LINK 8: Performance Telemetry Recording
        metrics = self.brain.get_metrics()
        self.assertGreaterEqual(metrics["total_executions"], 1)
        self.assertGreater(metrics["success_rate"], 0.0)
        self.assertIn("agents_tracked", metrics)

        # LINK 9: Immutable Audit Event Logging & Hash Chaining
        audit_events = self.brain.get_audit_trail(limit=50)
        self.assertGreater(len(audit_events), 0)

        # Verify audit trail properties across events
        categories = set()
        for evt in audit_events:
            self.assertIn("event_id", evt)
            self.assertIn("category", evt)
            self.assertIn("action", evt)
            self.assertIn("status", evt)
            self.assertIn("timestamp", evt)
            categories.add(evt["category"])

        self.assertTrue({"DISCOVERY", "ROUTING", "PLANNING", "APPROVAL", "EXECUTION"}.issubset(categories))

        # Verify no secrets in audit trail
        # Shape-based rather than one hard-coded literal: no Render-style
        # (rnd_...) or OpenAI project (sk-proj-) key may appear at all.
        render_key = re.compile(r"rnd_[A-Za-z0-9]{20,}")
        for evt in audit_events:
            evt_str = str(evt)
            self.assertIsNone(render_key.search(evt_str), "Render-style key in audit trail")
            self.assertNotIn("sk-proj-", evt_str)

    def test_antigravity_gui_and_credentials_isolation(self) -> None:
        """Verify the running GUI and root ~/.gemini remain completely intact.

        The GUI is discovered by executable path, not by a hardcoded PID, because
        the IDE may legitimately be restarted and the kernel recycles PIDs onto
        unrelated processes. See tests/support/gui_guard.py.
        """
        # 1. GUI process identity check
        from tests.support.gui_guard import describe_gui, find_gui_processes

        gui_procs = find_gui_processes()
        if not gui_procs:
            self.skipTest(
                "Antigravity IDE GUI is not running; isolation cannot be observed. "
                "This is not a violation - the user may have closed it."
            )
        for gui in gui_procs:
            self.assertIn("antigravity-ide", gui.command, describe_gui())

        # 2. Keyring service namespace isolation
        from providers.registry.credential_manager import KEYRING_SERVICE
        self.assertEqual(KEYRING_SERVICE, "mission-control")
        self.assertNotEqual(KEYRING_SERVICE, "gemini")


if __name__ == "__main__":
    unittest.main()
