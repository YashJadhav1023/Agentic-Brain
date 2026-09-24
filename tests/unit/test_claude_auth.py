"""Tests for Claude account authentication metadata handling.

These are hermetic: every profile is a temporary directory and no test invokes
the real `claude` CLI, reaches the network, or touches the developer's own
~/.claude profile.
"""
from __future__ import annotations

import json
import os
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agents.claude.auth import (
    CONFIG_DIR_ENV_VAR,
    ClaudeAuthMode,
    ensure_profile_dir,
    inspect_profile,
    login_command,
    logout_command,
    resolve_config_dir,
)


def _write_credentials(directory: Path, **oauth: object) -> Path:
    """Write a credential file shaped like the real one, with fake token values."""
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat01-FAKE-NOT-A-REAL-TOKEN",
            "refreshToken": "sk-ant-ort01-FAKE-NOT-A-REAL-TOKEN",
            "expiresAt": int((time.time() + 3600) * 1000),
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
            "scopes": ["user:inference", "user:profile"],
            **oauth,
        },
        "organizationUuid": "00000000-0000-0000-0000-000000000000",
    }
    path = directory / ".credentials.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


class TestAuthModeParsing(unittest.TestCase):
    def test_defaults_to_subscription(self) -> None:
        """An unset mode must default to the one that holds no secret."""
        for value in (None, "", "   ", "nonsense-mode"):
            self.assertIs(ClaudeAuthMode.parse(value), ClaudeAuthMode.SUBSCRIPTION)

    def test_recognises_aliases(self) -> None:
        cases = {
            "subscription": ClaudeAuthMode.SUBSCRIPTION,
            "Max": ClaudeAuthMode.SUBSCRIPTION,
            "pro": ClaudeAuthMode.SUBSCRIPTION,
            "oauth": ClaudeAuthMode.OAUTH_TOKEN,
            "oauth-token": ClaudeAuthMode.OAUTH_TOKEN,
            "setup token": ClaudeAuthMode.OAUTH_TOKEN,
            "api_key": ClaudeAuthMode.API_KEY,
            "API Key": ClaudeAuthMode.API_KEY,
            "key": ClaudeAuthMode.API_KEY,
        }
        for text, expected in cases.items():
            self.assertIs(ClaudeAuthMode.parse(text), expected, text)


class TestInspectProfile(unittest.TestCase):
    def test_missing_directory_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            status = inspect_profile(Path(tmp) / "absent")
            self.assertFalse(status.exists)
            self.assertFalse(status.logged_in)
            self.assertIn("does not exist", status.reason)

    def test_directory_without_credentials_is_not_logged_in(self) -> None:
        with TemporaryDirectory() as tmp:
            status = inspect_profile(tmp)
            self.assertTrue(status.exists)
            self.assertFalse(status.logged_in)
            self.assertIn("not logged in", status.reason)

    def test_live_credential_reports_plan_and_expiry(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp))
            status = inspect_profile(tmp)
            self.assertTrue(status.logged_in)
            self.assertEqual(status.plan, "max")
            self.assertEqual(status.rate_limit_tier, "default_claude_max_20x")
            self.assertIsNotNone(status.seconds_until_expiry)
            assert status.seconds_until_expiry is not None
            self.assertGreater(status.seconds_until_expiry, 0)
            self.assertFalse(status.expiring_soon)

    def test_expired_credential_is_not_logged_in(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), expiresAt=int((time.time() - 60) * 1000))
            status = inspect_profile(tmp)
            self.assertFalse(status.logged_in)
            self.assertIn("expired", status.reason)

    def test_expiry_close_to_now_is_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), expiresAt=int((time.time() + 60) * 1000))
            status = inspect_profile(tmp)
            self.assertTrue(status.logged_in)
            self.assertTrue(status.expiring_soon)

    def test_second_epoch_format_is_not_read_as_expired(self) -> None:
        """A seconds-precision expiry must not be divided by 1000 again.

        Treating a seconds value as milliseconds would place expiry in 1970 and
        report every live session as dead.
        """
        with TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp), expiresAt=int(time.time() + 3600))
            status = inspect_profile(tmp)
            self.assertTrue(status.logged_in)

    def test_absent_expiry_is_treated_as_live(self) -> None:
        """A missing field must not become a self-inflicted outage."""
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}}), encoding="utf-8")
            status = inspect_profile(tmp)
            self.assertTrue(status.logged_in)
            self.assertEqual(status.plan, "pro")

    def test_malformed_json_is_reported_not_raised(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / ".credentials.json").write_text("{not json", encoding="utf-8")
            status = inspect_profile(tmp)
            self.assertFalse(status.logged_in)
            self.assertIn("unreadable", status.reason)

    def test_api_key_only_profile_has_no_oauth_section(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / ".credentials.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
            status = inspect_profile(tmp)
            self.assertFalse(status.logged_in)
            self.assertIn("claudeAiOauth", status.reason)

    def test_world_readable_credential_is_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            path = _write_credentials(Path(tmp))
            path.chmod(0o644)
            self.assertTrue(inspect_profile(tmp).world_readable)

    def test_status_never_carries_token_material(self) -> None:
        """The serialized status is written to telemetry, so it must be clean."""
        with TemporaryDirectory() as tmp:
            _write_credentials(Path(tmp))
            blob = json.dumps(inspect_profile(tmp).to_dict())
            for forbidden in ("sk-ant", "accessToken", "refreshToken", "FAKE-NOT-A-REAL-TOKEN"):
                self.assertNotIn(forbidden, blob)


class TestResolveConfigDir(unittest.TestCase):
    def test_none_falls_back_to_the_shared_profile(self) -> None:
        saved = os.environ.pop(CONFIG_DIR_ENV_VAR, None)
        try:
            self.assertEqual(resolve_config_dir(None), Path.home() / ".claude")
        finally:
            if saved is not None:
                os.environ[CONFIG_DIR_ENV_VAR] = saved

    def test_environment_override_is_honoured(self) -> None:
        saved = os.environ.get(CONFIG_DIR_ENV_VAR)
        os.environ[CONFIG_DIR_ENV_VAR] = "/tmp/pxbrain-test-profile"
        try:
            self.assertEqual(resolve_config_dir(None), Path("/tmp/pxbrain-test-profile"))
        finally:
            if saved is None:
                os.environ.pop(CONFIG_DIR_ENV_VAR, None)
            else:
                os.environ[CONFIG_DIR_ENV_VAR] = saved

    def test_tilde_is_expanded(self) -> None:
        self.assertEqual(resolve_config_dir("~/x/y"), Path.home() / "x" / "y")


class TestEnsureProfileDir(unittest.TestCase):
    def test_creates_owner_only_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            created = ensure_profile_dir(Path(tmp) / "nested" / "account-2")
            self.assertTrue(created.is_dir())
            self.assertEqual(stat.S_IMODE(created.stat().st_mode) & 0o077, 0)

    def test_tightens_an_existing_loose_directory(self) -> None:
        """A credential must never be written into a world-readable directory."""
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "loose"
            target.mkdir(mode=0o777)
            ensure_profile_dir(target)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode) & 0o077, 0)


class TestLoginCommands(unittest.TestCase):
    def test_login_command_pins_the_profile_directory(self) -> None:
        command = login_command("/tmp/acct", executable="claude")
        self.assertIn(f'{CONFIG_DIR_ENV_VAR}="/tmp/acct"', command)
        self.assertIn("/login", command)

    def test_logout_command_pins_the_profile_directory(self) -> None:
        command = logout_command("/tmp/acct", executable="claude")
        self.assertIn(f'{CONFIG_DIR_ENV_VAR}="/tmp/acct"', command)
        self.assertIn("/logout", command)


if __name__ == "__main__":
    unittest.main()
