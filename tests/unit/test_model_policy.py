"""Deterministic model selection, verified catalogues, and honest verification."""
import json
import unittest
from pathlib import Path

from providers.registry.config import resolve_config_path
from agents.base.adapter import UNKNOWN_MODEL
from models.policies.model_policy import (
    AGENT_CATALOGS,
    Complexity,
    ModelTier,
    select_model,
    verify_actual_model,
)


class TestModelPolicy(unittest.TestCase):

    def test_frontier_selection_on_reasoning(self):
        dec_kiro = select_model("kiro-cli", complexity=Complexity.REASONING)
        self.assertEqual(dec_kiro.tier, ModelTier.FRONTIER)
        self.assertEqual(dec_kiro.preferred_model, "claude-opus-5")

        dec_a1 = select_model("antigravity-account-1", complexity=Complexity.REASONING)
        self.assertEqual(dec_a1.tier, ModelTier.FRONTIER)
        self.assertEqual(dec_a1.preferred_model, "claude-opus-4-6-thinking")

        dec_a2 = select_model("antigravity-account-2", complexity=Complexity.REASONING)
        self.assertEqual(dec_a2.tier, ModelTier.FRONTIER)
        self.assertEqual(dec_a2.preferred_model, "gemini-3.1-pro-high")

    def test_fast_selection_on_fast(self):
        dec_kiro = select_model("kiro-cli", complexity=Complexity.FAST)
        self.assertEqual(dec_kiro.tier, ModelTier.FAST)
        self.assertEqual(dec_kiro.preferred_model, "claude-haiku-4.5")

        dec_a1 = select_model("antigravity-account-1", complexity=Complexity.FAST)
        self.assertEqual(dec_a1.tier, ModelTier.FAST)
        self.assertEqual(dec_a1.preferred_model, "gemini-3.8-flash-low")

    def test_account2_is_not_pinned_to_a_single_model(self):
        """Requirement: the router picks the model; nothing is hard-coded."""
        chosen = {
            select_model("antigravity-account-2", complexity=c).preferred_model
            for c in Complexity
        }
        self.assertGreater(len(chosen), 1)
        self.assertNotEqual(chosen, {"gemini-3.8-flash-low"})

    def test_account2_catalog_matches_configured_models(self):
        """Catalogue and config must agree; both were verified against the CLI."""
        config = json.loads(resolve_config_path().read_text(encoding="utf-8"))
        configured = set(
            config["providers"]["antigravity"]["accounts"]["antigravity-account-2"]["models"]
        )
        catalog = {spec.name for spec in AGENT_CATALOGS["antigravity-account-2"]}
        self.assertEqual(catalog, configured)

    def test_both_accounts_expose_the_same_verified_catalog(self):
        a1 = {s.name for s in AGENT_CATALOGS["antigravity-account-1"]}
        a2 = {s.name for s in AGENT_CATALOGS["antigravity-account-2"]}
        self.assertEqual(a1, a2)

    def test_no_catalog_offers_an_unconfigured_model(self):
        """Every routable model must exist in the verified config catalogue."""
        from providers.registry.config import get_accounts

        configured: dict[str, set[str]] = {}
        for provider_id in ("antigravity", "kiro", "cline"):
            for account in get_accounts(provider_id):
                configured[account.agent_id] = set(account.models)

        for agent_id, models in configured.items():
            catalog = {spec.name for spec in AGENT_CATALOGS.get(agent_id, ())}
            self.assertTrue(
                catalog.issubset(models),
                f"{agent_id} catalogue offers unconfigured models: {catalog - models}",
            )

    def test_verify_actual_model_never_assumes(self):
        self.assertEqual(
            verify_actual_model({"actual_model": "gemini-3.8-flash-low"}, "claude-opus-5"),
            "gemini-3.8-flash-low",
        )
        self.assertEqual(
            verify_actual_model({"raw_response": {"model": "gemini-3.8-flash-medium"}}, None),
            "gemini-3.8-flash-medium",
        )

    def test_unreported_model_is_unknown_not_the_requested_model(self):
        """Requirement: never fabricate model information."""
        payload = {"status": "SUCCESS", "response": "ok", "usage": {}}
        self.assertEqual(verify_actual_model(payload, "gemini-3.8-flash-low"), UNKNOWN_MODEL)
        self.assertEqual(verify_actual_model({}, "auto"), UNKNOWN_MODEL)
        self.assertEqual(verify_actual_model("plain text", "gemini-3.1-pro-high"), UNKNOWN_MODEL)


if __name__ == "__main__":
    unittest.main()
