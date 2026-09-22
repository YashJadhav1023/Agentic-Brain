"""Cline must only ever reach free models.

Cline's effective provider lives in mutable state (`globalState.json`) that any
interactive session rewrites. On 2026-09-22 it had drifted back to the paid `cline`
provider on `moonshotai/kimi-k3`, with OpenRouter holding `anthropic/claude-fable-5`
at $10/M in and $50/M out. These tests assert the *outcome* — what reaches the
command line and what is written to disk — not merely that a setting exists.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agents.cline import free_routing
from agents.cline.adapter import ClineAdapter

PAID_IDS = (
    "moonshotai/kimi-k3",
    "anthropic/claude-fable-5",
    "z-ai/glm-5.3-flash",
    "deepseek/deepseek-v4-flash",
    "openai/gpt-5",
    "x-ai/grok-4",
)


class TestFreeModelCoercion(unittest.TestCase):
    def test_free_model_passes_through(self):
        for model in free_routing.FREE_MODELS:
            self.assertEqual(free_routing.coerce_model(model), model)

    def test_every_paid_id_is_coerced_to_a_free_model(self):
        for paid in PAID_IDS:
            got = free_routing.coerce_model(paid)
            self.assertIn(got, free_routing.FREE_MODELS, f"{paid} coerced to non-free {got}")

    def test_auto_is_coerced_rather_than_forwarded(self):
        # "auto" hands the choice back to Cline's persisted state, so it must not
        # survive as a literal model id.
        self.assertNotEqual(free_routing.coerce_model("auto"), "auto")
        self.assertIn(free_routing.coerce_model("auto"), free_routing.FREE_MODELS)

    def test_empty_and_none_yield_a_concrete_free_model(self):
        for empty in ("", None):
            self.assertIn(free_routing.coerce_model(empty), free_routing.FREE_MODELS)

    def test_allowlist_is_exact_not_prefix_matched(self):
        # A future paid tier must not qualify by looking Gemini-shaped.
        self.assertFalse(free_routing.is_free_model("gemini-9.9-ultra-paid"))
        self.assertFalse(free_routing.is_free_model("gemini-3.6-flash-preview-paid"))

    def test_free_models_only_never_returns_empty(self):
        self.assertEqual(free_routing.free_models_only(["anthropic/claude-fable-5"]),
                         (free_routing.DEFAULT_FREE_MODEL,))
        self.assertTrue(all(free_routing.is_free_model(m)
                            for m in free_routing.free_models_only(list(PAID_IDS))))


class TestAdapterCommandIsAlwaysFree(unittest.TestCase):
    """The constructed argv is the only thing the CLI actually obeys."""

    def _argv_for(self, requested_model):
        adapter = ClineAdapter(executable="cline")
        captured = {}

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return Proc()

        with mock.patch("shutil.which", return_value="/usr/bin/cline"), \
             mock.patch("subprocess.run", side_effect=fake_run), \
             mock.patch.object(free_routing, "pin_free_route", return_value=[]):
            adapter.execute("task-1", "do a thing", model=requested_model)
        return captured.get("cmd", [])

    def test_provider_is_pinned_to_the_free_provider(self):
        argv = self._argv_for("gemini-3.6-flash")
        self.assertIn("-P", argv, "no -P flag: Cline would use its persisted paid provider")
        self.assertEqual(argv[argv.index("-P") + 1], free_routing.FREE_PROVIDER)

    def test_a_model_is_always_sent_explicitly(self):
        # Omitting -m lets Cline fall back to persisted (possibly paid) state.
        for requested in ("auto", "", None, "gemini-3.6-flash"):
            argv = self._argv_for(requested)
            self.assertIn("-m", argv, f"no -m for requested={requested!r}")
            self.assertIn(argv[argv.index("-m") + 1], free_routing.FREE_MODELS)

    def test_no_paid_identifier_ever_reaches_the_command_line(self):
        for paid in PAID_IDS:
            argv = self._argv_for(paid)
            joined = " ".join(argv)
            self.assertNotIn(paid, joined, f"paid id {paid} leaked into argv")
            self.assertIn(argv[argv.index("-m") + 1], free_routing.FREE_MODELS)

    def test_identity_is_preserved_while_routing_provider_reports_the_free_route(self):
        adapter = ClineAdapter()
        # `provider` is the agent identity and dispatch keys off it
        # (providers/adapters/bridge.py), so it must stay "cline".
        self.assertEqual(adapter.provider, "cline")
        # The upstream route is reported separately, for cost attribution.
        self.assertEqual(adapter.routing_provider, free_routing.FREE_PROVIDER)


class TestAvailableModelsRefusesPaidDiscovery(unittest.TestCase):
    def test_paid_model_in_providers_json_is_not_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = data_dir / "settings"
            settings.mkdir(parents=True)
            # This is the real drift shape: a paid model parked on a provider record.
            (settings / "providers.json").write_text(json.dumps({
                "lastUsedProvider": "cline",
                "providers": {
                    "cline": {"settings": {"model": "moonshotai/kimi-k3"}},
                    "openrouter": {"settings": {"model": "anthropic/claude-fable-5"}},
                },
            }), encoding="utf-8")

            models = ClineAdapter(data_dir=data_dir).available_models()
            self.assertTrue(models, "available_models must never be empty")
            for m in models:
                self.assertTrue(free_routing.is_free_model(m), f"paid model offered: {m}")


class TestPinFreeRoute(unittest.TestCase):
    """globalState.json is what Cline actually reads; providers.json alone is not enough."""

    def _drifted_state(self, base: Path):
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings").mkdir(parents=True, exist_ok=True)
        (base / "globalState.json").write_text(json.dumps({
            "actModeApiProvider": "cline",
            "planModeApiProvider": "cline",
            "actModeClineModelId": "z-ai/glm-5.3-flash",
            "planModeClineModelId": "z-ai/glm-5.3-flash",
            "actModeOpenRouterModelId": "anthropic/claude-fable-5",
            "actModeOpenRouterModelInfo": {"inputPrice": 10, "outputPrice": 50},
        }), encoding="utf-8")
        (base / "settings" / "providers.json").write_text(json.dumps({
            "lastUsedProvider": "cline",
            "providers": {
                "cline": {"settings": {"model": "moonshotai/kimi-k3"}},
                "gemini": {"settings": {"model": "gemini-3.6-flash"}},
                "openrouter": {"settings": {"model": "deepseek/deepseek-chat-v3.1:free"}},
            },
        }), encoding="utf-8")

    def test_drifted_paid_state_is_repinned_to_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            self._drifted_state(base)

            changes = free_routing.pin_free_route(config_dir=Path(tmp), data_dir=base)
            self.assertTrue(changes, "drifted state produced no changes")

            state = json.loads((base / "globalState.json").read_text())
            self.assertEqual(state["actModeApiProvider"], free_routing.FREE_PROVIDER)
            self.assertEqual(state["planModeApiProvider"], free_routing.FREE_PROVIDER)
            self.assertEqual(state["actModeGeminiModelId"], free_routing.DEFAULT_FREE_MODEL)
            self.assertEqual(state["planModeGeminiModelId"], free_routing.DEFAULT_FREE_MODEL)
            # Paid ids parked on other providers must be cleared, not merely unselected.
            self.assertEqual(state["actModeClineModelId"], "")
            self.assertEqual(state["actModeOpenRouterModelId"], "")
            self.assertEqual(state["actModeOpenRouterModelInfo"], {})

            providers = json.loads((base / "settings" / "providers.json").read_text())
            self.assertEqual(providers["lastUsedProvider"], free_routing.FREE_PROVIDER)
            self.assertEqual(providers["providers"]["cline"]["settings"]["model"], "")
            self.assertEqual(providers["providers"]["openrouter"]["settings"]["model"], "")
            self.assertEqual(
                providers["providers"]["gemini"]["settings"]["model"],
                free_routing.DEFAULT_FREE_MODEL,
            )

    def test_pin_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            self._drifted_state(base)
            free_routing.pin_free_route(config_dir=Path(tmp), data_dir=base)
            self.assertEqual(
                free_routing.pin_free_route(config_dir=Path(tmp), data_dir=base),
                [],
                "second pin reported changes; enforcement is not idempotent",
            )

    def test_missing_state_files_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                free_routing.pin_free_route(config_dir=Path(tmp), data_dir=Path(tmp) / "data"),
                [],
            )

    def test_credentials_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "data"
            base.mkdir(parents=True)
            (base / "settings").mkdir(parents=True)
            (base / "globalState.json").write_text("{}", encoding="utf-8")
            (base / "settings" / "providers.json").write_text(json.dumps({
                "lastUsedProvider": "cline",
                "providers": {
                    "gemini": {"settings": {"model": "x", "apiKey": "SENTINEL_KEY_VALUE"}},
                },
            }), encoding="utf-8")

            free_routing.pin_free_route(config_dir=Path(tmp), data_dir=base)
            after = json.loads((base / "settings" / "providers.json").read_text())
            self.assertEqual(
                after["providers"]["gemini"]["settings"]["apiKey"],
                "SENTINEL_KEY_VALUE",
                "enforcement modified a credential",
            )


class TestEscapeHatch(unittest.TestCase):
    def test_enforcement_can_be_disabled(self):
        with mock.patch.dict(os.environ, {free_routing.ENV_ENFORCE: "0"}):
            self.assertFalse(free_routing.enforcement_enabled())
            # With enforcement off, an explicit paid choice is honoured again.
            self.assertEqual(
                free_routing.coerce_model("anthropic/claude-fable-5"),
                "anthropic/claude-fable-5",
            )
            self.assertEqual(free_routing.pin_free_route(), [])

    def test_enforcement_is_on_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(free_routing.ENV_ENFORCE, None)
            self.assertTrue(free_routing.enforcement_enabled())


class TestCatalogIsFreeOnly(unittest.TestCase):
    def test_model_policy_cline_catalog_offers_only_free_models(self):
        from models.policies.model_policy import CLINE_CATALOG

        self.assertTrue(CLINE_CATALOG, "CLINE_CATALOG is empty")
        for spec in CLINE_CATALOG:
            self.assertTrue(
                free_routing.is_free_model(spec.name),
                f"CLINE_CATALOG offers non-free model {spec.name!r}",
            )


if __name__ == "__main__":
    unittest.main()
