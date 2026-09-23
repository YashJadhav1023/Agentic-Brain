"""Phase 10: credential security guarantees.

Covers requirements 6 (API key storage) and 7 (secret redaction) of the Phase 10
test matrix, and proves the SECURITY section's negative guarantees:

* no secrets in `config/providers.json`
* no secrets in API/CLI-facing serialisations
* no secrets in exception traces
* Mission Control credentials never occupy the Antigravity `service=gemini`
  keyring namespace

These tests are hermetic: they never write to the real OS keyring. The keyring
backend is stubbed unavailable so the encrypted file store in a temp directory
is exercised instead.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from providers.registry.account_registry import (
    Account,
    AccountStatus,
    AuthenticationType,
)
from providers.registry.credential_manager import (
    KEYRING_SERVICE,
    CredentialManager,
    EncryptedFileStore,
    KeyringCredentialStore,
    SecretRedactor,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FAKE_SECRET = "sk-phase10-unit-test-secret-value-0123456789"


class _UnavailableKeyring(KeyringCredentialStore):
    """Keyring stub that reports unavailable so no real secret is ever written."""

    def is_available(self) -> bool:  # pragma: no cover - trivial override
        return False


class TestKeyringNamespaceIsolation(unittest.TestCase):
    def test_mission_control_namespace_is_the_dedicated_service(self):
        self.assertEqual(KEYRING_SERVICE, "mission-control")

    def test_default_keyring_store_uses_the_mission_control_service(self):
        self.assertEqual(KeyringCredentialStore().service, "mission-control")

    def test_no_source_file_writes_to_the_gemini_keyring_namespace(self):
        """`service=gemini` belongs to the Antigravity GUI and is read-only to us.

        A `secret-tool store` invocation naming that service anywhere in the
        codebase would risk clobbering the live IDE session.
        """
        offenders = []
        store_pattern = re.compile(r"secret-tool[^\n]*store[^\n]*gemini")
        for path in PROJECT_ROOT.rglob("*.py"):
            if "runtime/sandboxes" in str(path) or "/tests/" in str(path):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if store_pattern.search(text):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [], f"files write to service=gemini: {offenders}")


class TestNoSecretsInConfiguration(unittest.TestCase):
    """`config/providers.json` may hold references, never secret material."""

    SECRET_SHAPES = (
        re.compile(r"sk-[a-zA-Z0-9_\-]{20,}"),
        re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
        re.compile(r"gh[pousr]-[a-zA-Z0-9]{36,}"),
    )

    def setUp(self) -> None:
        self.config_path = PROJECT_ROOT / "config" / "providers.json"
        self.raw = self.config_path.read_text(encoding="utf-8")

    def test_config_is_valid_json(self):
        json.loads(self.raw)

    def test_config_contains_no_secret_shaped_values(self):
        for pattern in self.SECRET_SHAPES:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(
                    pattern.search(self.raw),
                    f"secret-shaped value found in {self.config_path.name}",
                )

    def test_every_credential_field_is_a_reference_uri_not_a_secret(self):
        cfg = json.loads(self.raw)

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if "credential" in key.lower() and isinstance(value, str) and value:
                        self.assertTrue(
                            value.startswith("secret://") or value.startswith("env://"),
                            f"{key} is not a reference URI: {value!r}",
                        )
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(cfg)


class TestSecretRedaction(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = SecretRedactor()

    def test_registered_secret_is_redacted_from_text(self):
        self.redactor.register_secret(FAKE_SECRET)
        out = self.redactor.redact(f"request failed using key {FAKE_SECRET} at endpoint")
        self.assertNotIn(FAKE_SECRET, out)
        self.assertIn(SecretRedactor.REDACTION_TOKEN, out)

    def test_unregistered_but_secret_shaped_values_are_still_redacted(self):
        out = self.redactor.redact("Authorization: Bearer abcdefghijklmnop0123456789")
        self.assertNotIn("abcdefghijklmnop0123456789", out)

    def test_sensitive_keys_are_redacted_by_name_regardless_of_value(self):
        payload = {
            "provider": "openai",
            "api_key": "plainlooking",
            "nested": {"authorization": "whatever", "safe": "keep-me"},
            "items": [{"token": "abc"}],
        }
        clean = self.redactor.redact_dict(payload)
        self.assertEqual(clean["provider"], "openai")
        self.assertEqual(clean["api_key"], SecretRedactor.REDACTION_TOKEN)
        self.assertEqual(clean["nested"]["authorization"], SecretRedactor.REDACTION_TOKEN)
        self.assertEqual(clean["nested"]["safe"], "keep-me")
        self.assertEqual(clean["items"][0]["token"], SecretRedactor.REDACTION_TOKEN)

    def test_telemetry_token_counters_survive_redaction(self):
        """Usage metrics and token counters must not be blanked to ***REDACTED***."""
        payload = {
            "total_tokens": 150000,
            "input_tokens": 140000,
            "output_tokens": 10000,
            "today_tokens": 5000,
            "today_usage_tokens": 5000,
            "token_metrics": {
                "known_tokens": 150000,
                "unknown_count": 2,
            },
            "token_efficiency": 0.95,
        }
        clean = self.redactor.redact_dict(payload)
        self.assertEqual(clean["total_tokens"], 150000)
        self.assertEqual(clean["input_tokens"], 140000)
        self.assertEqual(clean["output_tokens"], 10000)
        self.assertEqual(clean["today_tokens"], 5000)
        self.assertEqual(clean["today_usage_tokens"], 5000)
        self.assertEqual(clean["token_metrics"]["known_tokens"], 150000)
        self.assertEqual(clean["token_metrics"]["unknown_count"], 2)
        self.assertEqual(clean["token_efficiency"], 0.95)

    def test_credential_reference_survives_redaction(self):
        """The reference URI is a pointer, not a secret.

        Blanking it would defeat the reference architecture and leave the
        Accounts view unable to show which stored secret backs which account.
        """
        clean = self.redactor.redact_dict(
            {"credential_reference": "secret://mission-control/openai/personal"}
        )
        self.assertEqual(
            clean["credential_reference"], "secret://mission-control/openai/personal"
        )

    def test_env_reference_survives_redaction(self):
        clean = self.redactor.redact_dict({"credential_ref": "env://OPENAI_API_KEY"})
        self.assertEqual(clean["credential_ref"], "env://OPENAI_API_KEY")

    def test_authentication_type_survives_redaction(self):
        clean = self.redactor.redact_dict({"authentication_type": "api_key"})
        self.assertEqual(clean["authentication_type"], "api_key")

    def test_an_allowlisted_field_still_scrubs_real_secret_material(self):
        """The allowlist must not become a bypass."""
        self.redactor.register_secret(FAKE_SECRET)
        clean = self.redactor.redact_dict({"credential_reference": FAKE_SECRET})
        self.assertNotIn(FAKE_SECRET, str(clean["credential_reference"]))

    def test_an_allowlisted_field_scrubs_secret_shaped_material_too(self):
        clean = self.redactor.redact_dict(
            {"credential_reference": "sk-abcdefghijklmnopqrstuvwxyz0123456789"}
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(clean["credential_reference"]))

    def test_the_allowlist_stays_narrow(self):
        """Widening this set is a security decision and must be deliberate."""
        self.assertEqual(
            SecretRedactor.NON_SECRET_KEY_ALLOWLIST,
            frozenset(
                {
                    "credential_reference",
                    "credential_ref",
                    "authentication_type",
                    "auth_type",
                    # Deliberately widened in f97fc14 for the account wizard:
                    # these hold a login-method *name* ("api_key", "oauth",
                    # "workos_oauth"), never secret material, and the wizard UI
                    # cannot show which method was chosen if they are redacted.
                    # Values still pass value-level redaction (tested below).
                    "auth_method",
                    "login_method",
                    "sso_method",
                    # NOTE: matching lower-cases the key, so this camelCase entry
                    # never matches anything; "authMethod" keys are still
                    # redacted. Kept here only to pin the current set exactly.
                    "authMethod",
                }
            ),
        )

    def test_login_method_names_survive_redaction(self):
        clean = self.redactor.redact_dict(
            {"auth_method": "oauth", "login_method": "api_key", "sso_method": "builder_id"}
        )
        self.assertEqual(
            clean, {"auth_method": "oauth", "login_method": "api_key", "sso_method": "builder_id"}
        )

    def test_login_method_fields_still_scrub_secret_material(self):
        """Allowlisting the method-name keys must not become a bypass."""
        self.redactor.register_secret(FAKE_SECRET)
        clean = self.redactor.redact_dict(
            {
                "auth_method": FAKE_SECRET,
                "login_method": "sk-abcdefghijklmnopqrstuvwxyz0123456789",
                "sso_method": "Bearer abcdefghijklmnopqrstuvwxyz",
            }
        )
        blob = str(clean)
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", blob)

    def test_exception_traces_can_be_redacted_before_surfacing(self):
        self.redactor.register_secret(FAKE_SECRET)
        try:
            raise RuntimeError(f"upstream rejected key {FAKE_SECRET}")
        except RuntimeError as exc:
            self.assertNotIn(FAKE_SECRET, self.redactor.redact(str(exc)))


class TestSecureStorage(unittest.TestCase):
    def test_secret_is_not_recoverable_from_the_raw_store_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.dat"
            store = EncryptedFileStore(storage_path=path)
            ref = "secret://mission-control/openai/unit-test"
            self.assertTrue(store.store(ref, FAKE_SECRET))
            self.assertNotIn(FAKE_SECRET.encode(), path.read_bytes())
            self.assertEqual(store.retrieve(ref), FAKE_SECRET)

    def test_store_file_permissions_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.dat"
            store = EncryptedFileStore(storage_path=path)
            store.store("secret://mission-control/openai/unit-test", FAKE_SECRET)
            self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")

    def test_manager_round_trip_never_touches_the_real_keyring(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CredentialManager(
                keyring_store=_UnavailableKeyring(),
                file_store=EncryptedFileStore(storage_path=Path(tmp) / "creds.dat"),
            )
            ref = CredentialManager.generate_reference("openai", "unit-test")
            self.assertEqual(ref, "secret://mission-control/openai/unit-test")
            self.assertFalse(manager.exists(ref))
            self.assertTrue(manager.store(ref, FAKE_SECRET))
            self.assertTrue(manager.exists(ref))
            self.assertEqual(manager.retrieve(ref), FAKE_SECRET)
            self.assertTrue(manager.rotate(ref, FAKE_SECRET + "-rotated"))
            self.assertEqual(manager.retrieve(ref), FAKE_SECRET + "-rotated")
            self.assertTrue(manager.delete(ref))
            self.assertFalse(manager.exists(ref))
            self.assertIsNone(manager.retrieve(ref))

    def test_storing_an_empty_secret_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CredentialManager(
                keyring_store=_UnavailableKeyring(),
                file_store=EncryptedFileStore(storage_path=Path(tmp) / "creds.dat"),
            )
            self.assertFalse(
                manager.store("secret://mission-control/openai/empty", "")
            )

    def test_stored_secret_is_auto_registered_for_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CredentialManager(
                keyring_store=_UnavailableKeyring(),
                file_store=EncryptedFileStore(storage_path=Path(tmp) / "creds.dat"),
            )
            manager.store("secret://mission-control/openai/redact", FAKE_SECRET)
            self.assertNotIn(FAKE_SECRET, manager.redactor.redact(f"leak {FAKE_SECRET}"))


class TestAccountSerializationCarriesNoSecret(unittest.TestCase):
    def test_account_to_dict_exposes_the_reference_not_the_secret(self):
        acc = Account(
            id="openai-personal",
            provider_id="openai",
            account_name="personal",
            authentication_type=AuthenticationType.API_KEY,
            credential_reference="secret://mission-control/openai/personal",
            status=AccountStatus.ONLINE,
        )
        blob = json.dumps(acc.to_dict())
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertNotIn("sk-", blob)
        self.assertIn("secret://mission-control/openai/personal", blob)

    def test_account_object_has_no_attribute_holding_a_raw_secret(self):
        acc = Account(id="a", provider_id="p", account_name="n")
        for attr in vars(acc):
            self.assertNotIn(
                attr, ("api_key", "secret", "password", "token"),
                f"Account must not carry raw secret attribute {attr}",
            )


if __name__ == "__main__":
    unittest.main()
