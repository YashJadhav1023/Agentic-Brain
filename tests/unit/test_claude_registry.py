"""Registry wiring for the Claude provider.

Covers the contract between config/providers.json, the bootstrap registration
helpers, and what Mission Control ends up displaying. Hermetic: every case uses
a temporary providers.json and a temporary profile directory, so no test reads
the developer's real configuration or account.
"""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from providers.registry.account_registry import AuthenticationType
from providers.registry.bootstrap import (
    CLI_PROVIDER_IDS,
    SIMPLE_CLI_ADAPTERS,
    _claude_account_fields,
    _claude_adapter_kwargs,
    create_default_registry,
)
from providers.registry.config import CONFIG_PATH_ENV_VAR, load_config

#: The dashboard blanks any response key matching this, so account metadata keys
#: must avoid it or a harmless value renders as "***REDACTED***" in the browser.
REDACTED_KEY_PATTERN = re.compile(r"auth|token|secret|credential", re.IGNORECASE)


class _FakeAccountView:
    """Minimal stand-in for AccountConfigView with just the fields used here."""

    def __init__(self, raw: dict, agent_id: str = "claude-account-9", account_id: str = "account-9") -> None:
        self.agent_id = agent_id
        self.account_id = account_id
        self.raw = raw
        self.enabled = True
        self.command = "claude"
        self.capabilities = frozenset()
        self.models = ()
        self.default_model = "sonnet"


class _FakeAdapter:
    profile_dir = Path("/tmp/fake-profile")
    pxpipe_base_url = None

    def profile_status(self):
        from agents.claude.auth import ProfileStatus

        return ProfileStatus(
            config_dir=self.profile_dir,
            exists=True,
            logged_in=True,
            reason="logged in (max)",
            plan="max",
        )


def _write_config(directory: Path, account: dict, pxpipe_enabled: bool = True) -> Path:
    config = {
        "providers": {
            "claude": {
                "id": "claude",
                "name": "Claude Code",
                "enabled": True,
                "command": "claude",
                "profile_root": str(directory / "profiles"),
                "accounts": {account["agent_id"]: account},
            }
        },
        "integrations": {
            "pxpipe": {
                "enabled": pxpipe_enabled,
                "port": 47821,
                "events_file": str(directory / "events.jsonl"),
            }
        },
    }
    path = directory / "providers.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _logged_in(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    credentials = directory / ".credentials.json"
    credentials.write_text(
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
    credentials.chmod(0o600)
    return directory


class TestProviderIsRegistered(unittest.TestCase):
    def test_claude_is_a_cli_adapter(self) -> None:
        self.assertIn("claude", SIMPLE_CLI_ADAPTERS)

    def test_claude_is_excluded_from_the_direct_api_loop(self) -> None:
        """Otherwise it would be registered twice, the second time wrongly."""
        self.assertIn("claude", CLI_PROVIDER_IDS)


class TestAccountFields(unittest.TestCase):
    def test_subscription_mode_maps_to_its_own_auth_type(self) -> None:
        """SUBSCRIPTION, not OAUTH: it tells an auditor no secret is held here."""
        auth_type, _ = _claude_account_fields(_FakeAdapter(), _FakeAccountView({"auth_mode": "subscription"}))
        self.assertIs(auth_type, AuthenticationType.SUBSCRIPTION)

    def test_token_and_key_modes_map_to_their_types(self) -> None:
        cases = {
            "oauth_token": AuthenticationType.OAUTH,
            "api_key": AuthenticationType.API_KEY,
        }
        for mode, expected in cases.items():
            auth_type, _ = _claude_account_fields(_FakeAdapter(), _FakeAccountView({"auth_mode": mode}))
            self.assertIs(auth_type, expected, mode)

    def test_metadata_keys_survive_the_dashboard_redactor(self) -> None:
        """A non-secret must not render as "***REDACTED***" in Mission Control."""
        _, metadata = _claude_account_fields(_FakeAdapter(), _FakeAccountView({"auth_mode": "subscription"}))
        offenders = [key for key in metadata if REDACTED_KEY_PATTERN.search(key)]
        self.assertEqual(offenders, [], f"these keys would be redacted in the UI: {offenders}")
        self.assertEqual(metadata["login_mode"], "subscription")

    def test_metadata_carries_no_token_material(self) -> None:
        _, metadata = _claude_account_fields(_FakeAdapter(), _FakeAccountView({"auth_mode": "subscription"}))
        blob = json.dumps(metadata)
        for forbidden in ("sk-ant", "accessToken", "refreshToken", "FAKE"):
            self.assertNotIn(forbidden, blob)


class TestAdapterKwargs(unittest.TestCase):
    def test_pxpipe_is_opt_in_per_account(self) -> None:
        """Imaged context is lossy, so it must never be applied by default."""
        without = _claude_adapter_kwargs(_FakeAccountView({}), "http://127.0.0.1:47821")
        self.assertIsNone(without["pxpipe_base_url"])
        with_flag = _claude_adapter_kwargs(_FakeAccountView({"use_pxpipe": True}), "http://127.0.0.1:47821")
        self.assertEqual(with_flag["pxpipe_base_url"], "http://127.0.0.1:47821")

    def test_no_proxy_url_available_means_no_routing(self) -> None:
        kwargs = _claude_adapter_kwargs(_FakeAccountView({"use_pxpipe": True}), None)
        self.assertIsNone(kwargs["pxpipe_base_url"])


class TestRegistryFromConfig(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._saved = os.environ.get(CONFIG_PATH_ENV_VAR)
        load_config.cache_clear()

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop(CONFIG_PATH_ENV_VAR, None)
        else:
            os.environ[CONFIG_PATH_ENV_VAR] = self._saved
        load_config.cache_clear()
        self._tmp.cleanup()

    def test_enabled_account_is_registered_with_subscription_auth(self) -> None:
        root = Path(self._tmp.name)
        profile = _logged_in(root / "profiles" / "account-9")
        config = _write_config(
            root,
            {
                "agent_id": "claude-account-9",
                "account_id": "account-9",
                "enabled": True,
                "auth_mode": "subscription",
                "config_dir": str(profile),
                "use_pxpipe": True,
                "models": ["sonnet"],
                "default_model": "sonnet",
                "capabilities": ["deep-reasoning"],
            },
        )
        os.environ[CONFIG_PATH_ENV_VAR] = str(config)
        load_config.cache_clear()

        registry = create_default_registry(config)
        account = registry.account_registry.get_account("claude-account-9")
        self.assertIsNotNone(account)
        assert account is not None
        self.assertIs(account.authentication_type, AuthenticationType.SUBSCRIPTION)
        self.assertEqual(account.metadata["login_mode"], "subscription")
        self.assertEqual(account.provider_id, "claude")

    def test_disabled_account_is_not_registered(self) -> None:
        root = Path(self._tmp.name)
        config = _write_config(
            root,
            {
                "agent_id": "claude-account-off",
                "account_id": "account-off",
                "enabled": False,
                "auth_mode": "subscription",
                "config_dir": str(root / "profiles" / "off"),
            },
        )
        os.environ[CONFIG_PATH_ENV_VAR] = str(config)
        load_config.cache_clear()
        registry = create_default_registry(config)
        self.assertIsNone(registry.account_registry.get_account("claude-account-off"))


if __name__ == "__main__":
    unittest.main()
