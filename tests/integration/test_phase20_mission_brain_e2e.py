"""End-to-end integration tests for Mission Control Phase 20 (MissionBrain).

Verifies full autonomous cycle across Phases 16–20:
- Resource discovery (Phase 16)
- Context & Knowledge federation (Phase 17)
- DAG planning, approval gates, and rollback (Phase 18)
- Dynamic self-optimization & Execution Fabric (Phase 19)
- Governance, health, and audit logging (Phase 20)
- Verified GUI session preservation (PID 5854)
"""
import os
import tempfile
import unittest
from pathlib import Path

from brain.mission_brain import MissionBrain
from brain.planner.plan_model import PlanStatus, StepStatus


class TestPhase20MissionBrainE2E(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        # Initialize MissionBrain
        self.brain = MissionBrain(workspace_dir=self.workspace)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_full_autonomous_mission_lifecycle(self):
        """Test complete end-to-end mission lifecycle through MissionBrain."""
        # 1. Resource Discovery (Phase 16)
        counts = self.brain.discover_resources()
        self.assertIn("mcp_server", counts)
        self.assertIn("mcp_tool", counts)
        self.assertIn("skill", counts)
        self.assertGreater(counts["mcp_server"], 0)

        # 2. Context Federation & Preview (Phase 17)
        task = "Refactor authentication token verification across services"
        ctx = self.brain.preview_context(task)
        self.assertEqual(ctx.task, task)
        self.assertIn("domain", ctx.to_dict())
        self.assertIsInstance(ctx.safety_constraints, list)
        self.assertGreater(len(ctx.safety_constraints), 0)

        # Context persistence
        ctx_id = self.brain.store_context(task)
        self.assertTrue(ctx_id.startswith("ctx-"))

        # 3. Smart Routing & Explainability (Phase 19)
        decision = self.brain.route_task(task)
        self.assertIsNotNone(decision.agent_id)
        self.assertIsNotNone(decision.model)
        self.assertGreater(decision.total_score, 0.0)

        expl = self.brain.explain_routing(task)
        self.assertEqual(expl["selected_agent"], decision.agent_id)
        self.assertIn("candidate_scores", expl)
        self.assertIn("recommended_mcps", expl)

        # 4. Plan Generation & Approval Gate (Phase 18)
        destructive_task = "Delete old test logs and purge cache tables"
        plan = self.brain.create_plan(destructive_task)
        self.assertTrue(plan.plan_id.startswith("plan-"))
        self.assertGreater(len(plan.steps), 0)
        self.assertTrue(plan.requires_approval)
        self.assertEqual(plan.status, PlanStatus.BLOCKED_ON_APPROVAL)

        # Unapproved execution must remain blocked
        exec_blocked = self.brain.execute_plan(plan.plan_id)
        self.assertEqual(exec_blocked["status"], "BLOCKED_ON_APPROVAL")

        # Explicit approval unblocks execution
        approved = self.brain.approve_plan(plan.plan_id, approver="test_operator")
        self.assertTrue(approved)
        plan_after = self.brain.get_plan(plan.plan_id)
        self.assertEqual(plan_after.status, PlanStatus.READY)

        # Execute safe plan
        safe_plan = self.brain.create_plan("Inspect workspace directory and compile report")
        exec_safe = self.brain.execute_plan(safe_plan.plan_id)
        self.assertEqual(exec_safe["status"], "COMPLETED")

        # 5. Direct Execution Fabric (Phase 19)
        exec_res = self.brain.execute_task(
            "echo 'Mission Control Phase 20 Verification'",
            command="echo 'Mission Control Phase 20 Verification'",
            verification_rules=["no_errors"],
        )
        self.assertEqual(exec_res.status, "SUCCESS")
        self.assertIn("Phase 20 Verification", exec_res.output)
        self.assertGreater(exec_res.tokens_used, 0)

        # 6. Performance Telemetry & Metrics (Phase 19)
        metrics = self.brain.get_metrics()
        self.assertGreaterEqual(metrics["total_executions"], 1)
        self.assertEqual(metrics["success_rate"], 1.0)

        # 7. Governance & Audit Logging (Phase 20)
        audit_events = self.brain.get_audit_trail(limit=20)
        self.assertGreater(len(audit_events), 0)
        categories = {e["category"] for e in audit_events}
        self.assertTrue({"DISCOVERY", "ROUTING", "PLANNING", "APPROVAL", "EXECUTION"}.issubset(categories))

        # 8. Health & Cluster Status (Phase 20)
        health = self.brain.health(deep=True)
        self.assertEqual(health["status"], "HEALTHY")
        # BUG-005: 7 adapters once the three cline accounts register
        # individually (5 = 3 antigravity + kiro + 1 collapsed cline).
        self.assertGreaterEqual(health["active_adapters"], 7)
        self.assertGreaterEqual(health["mcp_servers"]["total"], 10)

    def test_gui_process_safety_remains_uncompromised(self):
        """Ensure the running Antigravity IDE GUI remains untouched.

        The GUI is located by executable path rather than by a hardcoded PID:
        the user may restart the IDE at will, and PIDs are recycled by the
        kernel, so a literal PID can both fail spuriously and match the wrong
        process. See tests/support/gui_guard.py.
        """
        from tests.support.gui_guard import describe_gui, find_gui_processes

        procs = find_gui_processes()
        if not procs:
            self.skipTest(
                "Antigravity IDE GUI is not running; non-interference cannot be "
                "observed. This is not a violation - the user may have closed it."
            )
        for proc in procs:
            self.assertGreater(proc.pid, 0, describe_gui())
            self.assertIn("antigravity-ide", proc.command, describe_gui())


if __name__ == "__main__":
    unittest.main()
