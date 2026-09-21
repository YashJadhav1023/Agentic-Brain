"""SmartRouter classification, capability scoring and explicit override."""
import unittest

from agents.base.adapter import Capability
from brain.router.smart_router import SmartRouter
from models.policies.model_policy import Complexity
from providers.registry.bootstrap import create_default_registry


class TestSmartRouter(unittest.TestCase):

    def setUp(self):
        self.registry = create_default_registry()
        self.router = SmartRouter(self.registry)

    # --- specialty routing is preserved -------------------------------
    def test_route_architecture_to_account1(self):
        dec = self.router.route("Draft architecture RFC for secure cross-agent communication")
        self.assertEqual(dec.agent_id, "antigravity-account-1")

    def test_route_refactoring_to_account2(self):
        dec = self.router.route("Refactor telemetry and component handlers across microservices")
        self.assertEqual(dec.agent_id, "antigravity-account-2")

    def test_route_terminal_to_kiro(self):
        dec = self.router.route("Run pytest suite and verify docker container health")
        self.assertEqual(dec.agent_id, "kiro-cli")

    def test_route_frontend_to_cline(self):
        dec = self.router.route("Fix CSS flexbox styling on the navigation layout")
        # BUG-005: cline now registers three isolated accounts; a frontend
        # styling task must resolve to one of them, not the legacy collapsed
        # "cline" identity.
        self.assertTrue(
            dec.agent_id.startswith("cline-account-"),
            f"frontend task routed to {dec.agent_id}, expected a cline account",
        )

    # --- Account 2 competes on merit ----------------------------------
    def test_account2_wins_review_without_being_named(self):
        """Requirement: the user must not have to specify Account 2 every time."""
        dec = self.router.route("Review this architecture")
        self.assertEqual(dec.agent_id, "antigravity-account-2")
        self.assertEqual(dec.account_id, "account-2")

    def test_account2_wins_the_final_success_condition_instruction(self):
        dec = self.router.route("Review and refactor this service")
        self.assertEqual(dec.agent_id, "antigravity-account-2")
        self.assertIsNotNone(dec.model)

    def test_every_healthy_agent_is_scored_as_a_candidate(self):
        candidates = self.router.score_candidates("Review and refactor this service")
        ids = {c.agent_id for c in candidates}
        # BUG-005: every configured account (incl. all three cline accounts)
        # must be scored as an individual candidate.
        self.assertTrue(
            {
                "antigravity-account-1", "antigravity-account-2",
                "antigravity-account-3", "kiro-cli",
                "cline-account-1", "cline-account-2", "cline-account-3",
            }.issubset(ids)
        )
        self.assertEqual(candidates, sorted(candidates, key=lambda c: c.score, reverse=True))

    def test_capabilities_are_inferred_from_the_task_not_the_agent(self):
        caps = self.router.infer_capabilities("Refactor the auth component and review the result")
        self.assertIn(Capability.EDITOR_REFACTORING, caps)
        self.assertIn(Capability.COMPONENT_REFACTORING, caps)
        self.assertIn(Capability.CODE_REVIEW, caps)

    def test_complexity_inference(self):
        self.assertEqual(self.router.infer_complexity("fix a typo"), Complexity.FAST)
        self.assertEqual(
            self.router.infer_complexity("restructure the whole service"), Complexity.STRONG
        )
        self.assertEqual(
            self.router.infer_complexity("deep reasoning about consistency"), Complexity.REASONING
        )

    # --- explicit selection -------------------------------------------
    def test_explicit_override(self):
        dec = self.router.route(
            "Any instruction",
            preferred_agent="antigravity-account-2",
            preferred_model="gemini-3.8-flash-low",
        )
        self.assertEqual(dec.agent_id, "antigravity-account-2")
        self.assertEqual(dec.model, "gemini-3.8-flash-low")
        self.assertIn("Explicitly selected", dec.reason)

    def test_explicit_override_of_unknown_agent_fails_loudly(self):
        with self.assertRaises(RuntimeError):
            self.router.route("Any instruction", preferred_agent="antigravity-account-9")

    def test_decision_records_a_fallback(self):
        dec = self.router.route("Review and refactor this service")
        self.assertIsNotNone(dec.fallback_agent_id)
        self.assertNotEqual(dec.fallback_agent_id, dec.agent_id)


if __name__ == "__main__":
    unittest.main()
