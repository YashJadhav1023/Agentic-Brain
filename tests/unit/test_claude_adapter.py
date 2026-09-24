"""Tests for the Claude Code headless adapter.

Hermetic: no test spawns the real CLI. Parsing is exercised by handing the
adapter a synthetic CompletedProcess, and environment construction is inspected
directly, so the suite never spends a model request or touches a real account.

The payloads used here are the shapes observed from `claude -p --output-format
json` on 2.1.280, including the rate-limited one that exits 0 and reports
`subtype: "success"` while `is_error` is true.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agents.base.adapter import UNKNOWN_MODEL, Capability, ExecutionMode
from agents.claude.adapter import ClaudeAdapter, classify_failure
from agents.claude.auth import CONFIG_DIR_ENV_VAR, ClaudeAuthMode

SUCCESS_PAYLOAD = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "OK",
    "session_id": "0f85a35c-21c6-4a65-a4c2-9d2d3aedff2e",
    "num_turns": 1,
    "duration_ms": 1500,
    "total_cost_usd": 0.004,
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 120,
        "output_tokens": 8,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 900,
        "output_tokens_details": {"thinking_tokens": 3},
    },
}

#: Verified live: a quota wall exits 0 and mislabels subtype as success.
RATE_LIMITED_PAYLOAD = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": 429,
    "result": "You've hit your weekly limit \u00b7 resets Sep 26, 10:30am (Asia/Kolkata)",
    "session_id": "d624b2b0-7221-48ed-b5fc-f820f8d2cb55",
    "num_turns": 1,
    "duration_ms": 876,
    "usage": {"input_tokens": 0, "output_tokens": 0},
}


def _completed(stdout: object, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=text, stderr=stderr)


def _logged_in_profile(directory: Path) -> Path:
    path = directory / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat01-FAKE",
                    "expiresAt": int((time.time() + 3600) * 1000),
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return directory


class TestFailureClassification(unittest.TestCase):
    def test_quota_statuses_are_recoverable(self) -> None:
        """Rate limits must be distinguishable so failover moves accounts."""
        self.assertEqual(classify_failure(429, "whatever"), "rate_limit")
        self.assertEqual(classify_failure(529, "overloaded"), "rate_limit")

    def test_credential_statuses_are_not_recoverable_elsewhere(self) -> None:
        self.assertEqual(classify_failure(401, "nope"), "auth")
        self.assertEqual(classify_failure(403, "nope"), "auth")

    def test_message_markers_classify_without_a_status(self) -> None:
        self.assertEqual(classify_failure(None, "You've hit your weekly limit"), "rate_limit")
        self.assertEqual(classify_failure(None, "RESOURCE_EXHAUSTED"), "rate_limit")
        self.assertEqual(classify_failure(None, "Please run /login"), "auth")

    def test_unknown_failures_are_plain_errors(self) -> None:
        self.assertEqual(classify_failure(500, "internal"), "error")
        self.assertEqual(classify_failure(None, "syntax error in tool call"), "error")


class TestResultParsing(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.adapter = ClaudeAdapter(
            agent_id="claude-test",
            account_id="test",
            config_dir=self._tmp.name,
            models=("sonnet",),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _parse(self, proc: subprocess.CompletedProcess[str]):
        return self.adapter._parse(
            task_id="t1",
            proc=proc,
            cmd=["claude", "-p", "--output-format", "json", "secret prompt"],
            requested_model="sonnet",
            duration=1.0,
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
        )

    def test_successful_run_is_parsed(self) -> None:
        result = self._parse(_completed(SUCCESS_PAYLOAD))
        self.assertTrue(result.success)
        self.assertEqual(result.output, "OK")
        self.assertEqual(result.error, "")
        self.assertEqual(result.session_id, SUCCESS_PAYLOAD["session_id"])
        self.assertEqual(result.input_tokens, 120)
        self.assertEqual(result.output_tokens, 8)
        self.assertEqual(result.cache_read_tokens, 900)
        self.assertEqual(result.thinking_tokens, 3)
        # 120 + 8 + 900 cache read + 40 cache create
        self.assertEqual(result.total_tokens, 1068)
        self.assertEqual(result.usage_source, "provider")
        self.assertEqual(result.provider_duration_seconds, 1.5)
        self.assertTrue(result.json_valid)

    def test_exit_zero_with_is_error_is_a_failure(self) -> None:
        """The core bug: a rate-limited run exits 0, so the exit code lies.

        Trusting it would record a quota wall as a completed task and hand the
        error string downstream as if it were the model's answer.
        """
        result = self._parse(_completed(RATE_LIMITED_PAYLOAD, returncode=0))
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "")
        self.assertIn("weekly limit", result.error)

    def test_subtype_success_does_not_override_is_error(self) -> None:
        """Verified live: subtype said success while is_error was true."""
        result = self._parse(_completed(RATE_LIMITED_PAYLOAD))
        self.assertEqual(result.provider_status, "success")
        self.assertFalse(result.success)

    def test_rate_limit_is_classified_for_failover(self) -> None:
        result = self._parse(_completed(RATE_LIMITED_PAYLOAD))
        self.assertEqual(result.raw_response.get("failure_class"), "rate_limit")

    def test_non_json_output_is_a_failure(self) -> None:
        result = self._parse(_completed("command not found", returncode=127, stderr="boom"))
        self.assertFalse(result.success)
        self.assertFalse(result.json_valid)
        self.assertIn("boom", result.error)

    def test_empty_result_text_is_not_success(self) -> None:
        payload = {**SUCCESS_PAYLOAD, "result": "   "}
        self.assertFalse(self._parse(_completed(payload)).success)

    def test_prompt_is_redacted_from_the_recorded_command(self) -> None:
        result = self._parse(_completed(SUCCESS_PAYLOAD))
        self.assertNotIn("secret prompt", " ".join(result.command))
        self.assertIn("<prompt redacted>", result.command)

    def test_actual_model_is_never_asserted(self) -> None:
        """The CLI does not echo the served model, so it must stay the sentinel."""
        self.assertEqual(self._parse(_completed(SUCCESS_PAYLOAD)).actual_model, UNKNOWN_MODEL)


class TestEnvironmentConstruction(unittest.TestCase):
    def test_config_dir_isolates_the_account(self) -> None:
        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(config_dir=tmp)
            self.assertEqual(adapter._build_env()[CONFIG_DIR_ENV_VAR], tmp)

    def test_ambient_credentials_cannot_hijack_a_subscription_account(self) -> None:
        """An exported ANTHROPIC_API_KEY would otherwise bill every account to it."""
        saved = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-be-used"
        try:
            with TemporaryDirectory() as tmp:
                env = ClaudeAdapter(config_dir=tmp, auth_mode=ClaudeAuthMode.SUBSCRIPTION)._build_env()
                self.assertNotIn("ANTHROPIC_API_KEY", env)
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        finally:
            if saved is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_api_key_mode_injects_the_resolved_secret(self) -> None:
        class Resolver:
            def resolve(self, _ref: str) -> str:
                return "sk-ant-resolved"

        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(
                config_dir=tmp,
                auth_mode=ClaudeAuthMode.API_KEY,
                credential_reference="secret://mission-control/claude/x",
                credential_resolver=Resolver(),
            )
            self.assertEqual(adapter._build_env()["ANTHROPIC_API_KEY"], "sk-ant-resolved")

    def test_oauth_token_mode_uses_the_token_variable(self) -> None:
        class Resolver:
            def resolve(self, _ref: str) -> str:
                return "sk-ant-oat01-resolved"

        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(
                config_dir=tmp,
                auth_mode=ClaudeAuthMode.OAUTH_TOKEN,
                credential_reference="secret://mission-control/claude/y",
                credential_resolver=Resolver(),
            )
            env = adapter._build_env()
            self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sk-ant-oat01-resolved")
            self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_a_failing_credential_backend_does_not_raise(self) -> None:
        class Broken:
            def resolve(self, _ref: str) -> str:
                raise RuntimeError("keyring is locked")

        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with TemporaryDirectory() as tmp:
                adapter = ClaudeAdapter(
                    config_dir=tmp,
                    auth_mode=ClaudeAuthMode.API_KEY,
                    credential_reference="secret://x",
                    credential_resolver=Broken(),
                )
                self.assertNotIn("ANTHROPIC_API_KEY", adapter._build_env())
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_pxpipe_base_url_is_only_set_when_configured(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertNotIn("ANTHROPIC_BASE_URL", ClaudeAdapter(config_dir=tmp)._build_env())
            routed = ClaudeAdapter(config_dir=tmp, pxpipe_base_url="http://127.0.0.1:47821")
            self.assertEqual(routed._build_env()["ANTHROPIC_BASE_URL"], "http://127.0.0.1:47821")


class TestHealth(unittest.TestCase):
    def test_not_logged_in_returns_an_actionable_message(self) -> None:
        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(config_dir=tmp, executable="sh")
            healthy, reason = adapter.health()
            self.assertFalse(healthy)
            self.assertIn("/login", reason)

    def test_logged_in_profile_is_healthy(self) -> None:
        with TemporaryDirectory() as tmp:
            _logged_in_profile(Path(tmp))
            healthy, reason = ClaudeAdapter(config_dir=tmp, executable="sh").health()
            self.assertTrue(healthy)
            self.assertIn("max", reason)

    def test_missing_executable_is_reported_first(self) -> None:
        adapter = ClaudeAdapter(executable="definitely-not-a-real-binary-xyz")
        healthy, reason = adapter.health()
        self.assertFalse(healthy)
        self.assertIn("not found", reason)

    def test_execute_refuses_before_spawning_when_unhealthy(self) -> None:
        """An unhealthy account must fail with the login hint, not a CLI error."""
        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(config_dir=tmp, executable="sh")
            result = adapter.execute(task_id="t", prompt="hi")
            self.assertFalse(result.success)
            self.assertEqual(result.raw_response.get("failure_class"), "auth")
            self.assertIn("/login", result.error)


class TestIdentityAndDescribe(unittest.TestCase):
    def test_identity_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            adapter = ClaudeAdapter(agent_id="claude-account-9", account_id="account-9", config_dir=tmp)
            self.assertEqual(adapter.provider, "claude")
            self.assertEqual(adapter.agent_id, "claude-account-9")
            self.assertEqual(adapter.account_id, "account-9")
            self.assertIs(adapter.execution_mode, ExecutionMode.HEADLESS)
            self.assertEqual(adapter.profile_dir, Path(tmp))
            self.assertIn(Capability.DEEP_REASONING, adapter.capabilities())

    def test_describe_exposes_auth_state_without_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            _logged_in_profile(Path(tmp))
            described = ClaudeAdapter(config_dir=tmp, executable="sh").describe()
            self.assertEqual(described["auth_mode"], "subscription")
            self.assertTrue(described["subscription"]["logged_in"])
            blob = json.dumps(described)
            for forbidden in ("sk-ant", "accessToken", "FAKE"):
                self.assertNotIn(forbidden, blob)

    def test_multi_account_is_declared(self) -> None:
        """Accounts are isolated by CLAUDE_CONFIG_DIR, so this must be true."""
        self.assertTrue(ClaudeAdapter.multi_account)


if __name__ == "__main__":
    unittest.main()
