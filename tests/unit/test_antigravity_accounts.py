"""Antigravity Accounts: discovery, isolation, command construction, parsing."""
import json
import unittest
from pathlib import Path

from agents.antigravity.account1.adapter import AntigravityAccount1Adapter
from agents.antigravity.account2.adapter import AntigravityAccount2Adapter
from agents.antigravity.account3.adapter import AntigravityAccount3Adapter
from agents.antigravity.adapter import (
    DEFAULT_CONFIG_PATH,
    AntigravityAccountAdapter,
    AntigravityAdapter,
)
from providers.registry.config import resolve_config_path
from agents.base.adapter import UNKNOWN_MODEL, AgentStatus, Capability, ExecutionMode


class TestAntigravityDiscovery(unittest.TestCase):
    """Requirement: All 3 accounts discovery."""

    def test_all_accounts_discovered_from_config(self):
        adapters = AntigravityAdapter.load_from_config()
        ids = sorted(a.agent_id for a in adapters)
        self.assertEqual(ids, ["antigravity-account-1", "antigravity-account-2", "antigravity-account-3"])

    def test_config_is_the_single_source_of_truth(self):
        self.assertTrue(DEFAULT_CONFIG_PATH.is_file())
        # Account assertions run against the resolved (hermetic fixture) config,
        # the same file load_from_config() reads.
        data = json.loads(resolve_config_path().read_text(encoding="utf-8"))
        accounts = data["providers"]["antigravity"]["accounts"]
        self.assertEqual(accounts["antigravity-account-1"]["execution"]["app_data_dir"], "antigravity-account-jadhav")
        self.assertEqual(accounts["antigravity-account-2"]["execution"]["app_data_dir"], "antigravity-account-3")
        self.assertEqual(accounts["antigravity-account-3"]["execution"]["app_data_dir"], "antigravity-account-yash")
        self.assertEqual(accounts["antigravity-account-2"]["execution"]["output_format"], "json")

    def test_missing_account_raises_rather_than_defaulting(self):
        with self.assertRaises(RuntimeError):
            AntigravityAdapter.get_account("antigravity-account-99")


class TestAntigravityAccounts(unittest.TestCase):

    def setUp(self):
        self.a1 = AntigravityAccount1Adapter()
        self.a2 = AntigravityAccount2Adapter()
        self.a3 = AntigravityAccount3Adapter()

    def test_account_isolation(self):
        """Requirement: account isolation. Profiles must never be shared."""
        self.assertEqual(self.a1.agent_id, "antigravity-account-1")
        self.assertEqual(self.a2.agent_id, "antigravity-account-2")
        self.assertEqual(self.a3.agent_id, "antigravity-account-3")
        self.assertEqual(self.a1.account_id, "account-1")
        self.assertEqual(self.a2.account_id, "account-2")
        self.assertEqual(self.a3.account_id, "account-3")
        self.assertEqual(self.a1.data_dir, "antigravity-account-jadhav")
        self.assertEqual(self.a2.data_dir, "antigravity-account-3")
        self.assertEqual(self.a3.data_dir, "antigravity-account-yash")
        dirs = {self.a1.data_dir, self.a2.data_dir, self.a3.data_dir}
        self.assertEqual(len(dirs), 3)
        profiles = {self.a1.profile_dir, self.a2.profile_dir, self.a3.profile_dir}
        self.assertEqual(len(profiles), 3)

    def test_all_accounts_are_headless(self):
        self.assertEqual(self.a1.execution_mode, ExecutionMode.HEADLESS)
        self.assertEqual(self.a2.execution_mode, ExecutionMode.HEADLESS)
        self.assertEqual(self.a3.execution_mode, ExecutionMode.HEADLESS)

    def test_account_capabilities(self):
        caps2 = self.a2.capabilities()
        self.assertIn(Capability.COMPONENT_REFACTORING, caps2)
        self.assertIn(Capability.CODE_REVIEW, caps2)
        caps3 = self.a3.capabilities()
        self.assertIn(Capability.DEEP_REASONING, caps3)

    def test_health_checks(self):
        """Requirement: Account health (lightweight, no model spend)."""
        ok1, reason1 = self.a1.health()
        self.assertTrue(ok1, f"Account 1 unhealthy: {reason1}")
        ok2, reason2 = self.a2.health()
        self.assertTrue(ok2, f"Account 2 unhealthy: {reason2}")
        ok3, reason3 = self.a3.health()
        self.assertTrue(ok3, f"Account 3 unhealthy: {reason3}")

    def test_status_and_cancel(self):
        self.assertEqual(self.a2.status(), AgentStatus.IDLE)
        self.assertTrue(self.a2.cancel("task-not-running"))

    def test_describe_exposes_registry_contract(self):
        described = self.a2.describe()
        for key in (
            "agent_id", "provider", "account_id", "profile", "execution_mode",
            "status", "capabilities", "models", "health", "data_dir",
            "dangerously_skip_permissions",
        ):
            self.assertIn(key, described)
        self.assertFalse(described["dangerously_skip_permissions"])


class TestCommandConstruction(unittest.TestCase):
    """Requirement: least privilege, and one place that builds the command line."""

    def setUp(self):
        self.a2 = AntigravityAccount2Adapter()

    def test_app_data_dir_always_present(self):
        argv = self.a2._base_argv(skip_permissions=False)
        self.assertIn("--app_data_dir=antigravity-account-3", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)

    def test_skip_permissions_off_by_default(self):
        argv = self.a2._base_argv(skip_permissions=False)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertFalse(self.a2._resolve_skip_permissions(None))
        self.assertFalse(self.a2._resolve_skip_permissions({}))

    def test_skip_permissions_only_when_explicitly_requested(self):
        self.assertTrue(
            self.a2._resolve_skip_permissions({"dangerously_skip_permissions": True})
        )
        argv = self.a2._base_argv(skip_permissions=True)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_prompt_is_redacted_from_recorded_command(self):
        redacted = self.a2._redact_argv(["agy", "-p", "secret prompt text"])
        self.assertNotIn("secret prompt text", redacted)
        self.assertIn("<prompt redacted>", redacted)


class TestJsonNormalization(unittest.TestCase):
    """Requirement: structural JSON parsing, never fragile string parsing."""

    def setUp(self):
        self.a2 = AntigravityAccount2Adapter()

    def _normalize(self, stdout, returncode=0, stderr=""):
        return self.a2._normalize(
            task_id="task-test",
            proc_returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            requested_model="gemini-3.8-flash-low",
            duration=1.5,
            argv=["agy", "-p", "x"],
            session_id="sess-test",
            fallback_conversation_id=None,
        )

    def test_parses_verified_live_payload_shape(self):
        payload = {
            "conversation_id": "612548ad-84d1-4d32-96b5-c68d97bbef6b",
            "status": "SUCCESS",
            "response": "ACCOUNT2_INTEGRATION_TEST_PASSED\n",
            "duration_seconds": 5.576408773,
            "num_turns": 1,
            "usage": {
                "input_tokens": 14535,
                "output_tokens": 80,
                "thinking_tokens": 69,
                "cache_read_tokens": 8150,
                "total_tokens": 14615,
            },
        }
        result = self._normalize(json.dumps(payload))
        self.assertTrue(result.success)
        self.assertTrue(result.json_valid)
        self.assertEqual(result.conversation_id, "612548ad-84d1-4d32-96b5-c68d97bbef6b")
        self.assertEqual(result.output.strip(), "ACCOUNT2_INTEGRATION_TEST_PASSED")
        self.assertEqual(result.input_tokens, 14535)
        self.assertEqual(result.thinking_tokens, 69)
        self.assertEqual(result.cache_read_tokens, 8150)
        self.assertEqual(result.total_tokens, 14615)
        self.assertEqual(result.num_turns, 1)
        self.assertEqual(result.provider_status, "SUCCESS")
        self.assertEqual(result.provider_duration_seconds, 5.576408773)

    def test_model_is_never_fabricated(self):
        """Requirement: the payload has no model field, so actual_model=unknown."""
        result = self._normalize(json.dumps({"status": "SUCCESS", "response": "hi"}))
        self.assertEqual(result.actual_model, UNKNOWN_MODEL)
        self.assertEqual(result.requested_model, "gemini-3.8-flash-low")

    def test_model_is_used_when_provider_reports_one(self):
        result = self._normalize(
            json.dumps({"status": "SUCCESS", "response": "hi", "model": "gemini-3.1-pro-high"})
        )
        self.assertEqual(result.actual_model, "gemini-3.1-pro-high")

    def test_empty_response_is_not_success(self):
        """Headless mode auto-denies tool permissions, exits 0 and says nothing."""
        result = self._normalize(
            json.dumps({"status": "SUCCESS", "response": "", "conversation_id": "abc"}),
            stderr='no output produced — a tool required the "command" permission',
        )
        self.assertFalse(result.success)
        self.assertIn("permission", result.error)
        self.assertIn("--allow-tool-permissions", result.error)

    def test_empty_response_without_stderr_still_fails(self):
        result = self._normalize(json.dumps({"status": "SUCCESS", "response": "   "}))
        self.assertFalse(result.success)
        self.assertIn("no output", result.error.lower())

    def test_soft_failure_status_is_not_reported_as_success(self):
        result = self._normalize(json.dumps({"status": "ERROR", "response": "boom"}))
        self.assertFalse(result.success)
        self.assertIn("ERROR", result.error)

    def test_nonzero_exit_is_failure(self):
        result = self._normalize("", returncode=1, stderr="quota exhausted")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "quota exhausted")

    def test_non_json_output_degrades_safely(self):
        result = self._normalize("plain text answer")
        self.assertFalse(result.json_valid)
        self.assertEqual(result.output, "plain text answer")

    def test_stream_json_lines_take_last_object(self):
        lines = "\n".join([
            json.dumps({"type": "delta", "text": "partial"}),
            json.dumps({"status": "SUCCESS", "response": "final", "conversation_id": "abc"}),
        ])
        result = self._normalize(lines)
        self.assertTrue(result.json_valid)
        self.assertEqual(result.output, "final")
        self.assertEqual(result.conversation_id, "abc")

    def test_raw_metadata_is_preserved(self):
        raw = json.dumps({"status": "SUCCESS", "response": "hi"})
        result = self._normalize(raw, stderr="a warning")
        self.assertEqual(result.raw_stdout, raw)
        self.assertEqual(result.raw_stderr, "a warning")
        self.assertTrue(result.command)

    def test_normalized_schema_shape(self):
        result = self._normalize(json.dumps({"status": "SUCCESS", "response": "hi"}))
        schema = result.normalized()
        for key in (
            "agent_id", "account_id", "task_id", "session_id", "conversation_id",
            "requested_model", "actual_model", "response", "usage",
            "duration_seconds", "exit_code", "status",
        ):
            self.assertIn(key, schema)
        self.assertEqual(schema["agent_id"], "antigravity-account-2")
        self.assertEqual(schema["account_id"], "account-2")
        self.assertEqual(schema["status"], "completed")


class TestModelDiscovery(unittest.TestCase):

    def test_parses_models_listing(self):
        stdout = (
            "Fetching available models...\n"
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
            "claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
        )
        models = AntigravityAccountAdapter.discover_models(stdout)
        self.assertEqual(models, ("gemini-3.8-flash-high", "claude-opus-4-6-thinking"))


class TestMissingBinaryHandling(unittest.TestCase):
    """Requirement: failure handling."""

    def test_execute_without_binary_fails_cleanly(self):
        adapter = AntigravityAccount2Adapter()
        adapter._bin = None
        result = adapter.execute(task_id="task-x", prompt="hello")
        self.assertFalse(result.success)
        self.assertEqual(result.actual_model, UNKNOWN_MODEL)
        self.assertIn("not resolved", result.error)


if __name__ == "__main__":
    unittest.main()
