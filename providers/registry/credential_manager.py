"""Secure Credential Manager and Secret Redaction for Mission Control.

Ensures credentials, API keys, and tokens are stored securely (OS Keyring,
encrypted local store, or environment references) and NEVER exposed in
configuration files, Git, logs, terminal outputs, or test fixtures.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

#: Namespaces used to separate Mission Control secrets from system/IDE services.
KEYRING_SERVICE = "mission-control"
DEFAULT_STORE_DIR = Path.home() / ".config" / "agentic_brain"
DEFAULT_STORE_FILE = DEFAULT_STORE_DIR / "credentials.dat"


class BaseCredentialStore(abc.ABC):
    """Abstract storage backend for secrets."""

    @abc.abstractmethod
    def store(self, key: str, secret: str) -> bool:
        ...

    @abc.abstractmethod
    def retrieve(self, key: str) -> str | None:
        ...

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        ...


class KeyringCredentialStore(BaseCredentialStore):
    """OS Keyring store using Linux secret-tool (libsecret)."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service
        self._bin = shutil.which("secret-tool")

    def is_available(self) -> bool:
        if not self._bin:
            return False
        # Test if secret-tool responds without error (requires active session bus)
        try:
            p = subprocess.run(
                [self._bin, "search", "--all", "service", "probe-probe-probe"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return p.returncode == 0 or "invalid bus" not in (p.stderr or "").lower()
        except Exception:
            return False

    def store(self, key: str, secret: str) -> bool:
        if not self._bin:
            return False
        try:
            p = subprocess.run(
                [
                    self._bin,
                    "store",
                    f"--label=Mission Control: {key}",
                    "service",
                    self.service,
                    "account",
                    key,
                ],
                input=secret + "\n",
                capture_output=True,
                text=True,
                timeout=5,
            )
            return p.returncode == 0
        except Exception:
            return False

    def retrieve(self, key: str) -> str | None:
        if not self._bin:
            return None
        try:
            p = subprocess.run(
                [self._bin, "lookup", "service", self.service, "account", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if p.returncode == 0 and p.stdout:
                return p.stdout.rstrip("\r\n")
            return None
        except Exception:
            return None

    def delete(self, key: str) -> bool:
        if not self._bin:
            return False
        try:
            p = subprocess.run(
                [self._bin, "clear", "service", self.service, "account", key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return p.returncode == 0
        except Exception:
            return False

    def exists(self, key: str) -> bool:
        return self.retrieve(key) is not None


class EncryptedFileStore(BaseCredentialStore):
    """Secure encrypted/obfuscated local file store with 0600 permissions."""

    def __init__(
        self,
        path: Path | str | None = None,
        storage_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path or storage_path or DEFAULT_STORE_FILE)
        self._key = self._derive_key()

    def _derive_key(self) -> bytes:
        # Machine-unique key derived from machine-id / host identity and user salt
        seed = b"mission-control-key-seed-v1"
        try:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.exists(p):
                    with open(p, "rb") as f:
                        seed += f.read().strip()
                        break
        except Exception:
            pass
        seed += os.path.expanduser("~").encode("utf-8")
        return hashlib.pbkdf2_hmac("sha256", seed, b"agentic-brain-salt", 100_000)

    def _xor_cipher(self, data: bytes, key: bytes) -> bytes:
        out = bytearray(len(data))
        klen = len(key)
        for i in range(len(data)):
            out[i] = data[i] ^ key[i % klen]
        return bytes(out)

    def _load_data(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            raw = self.path.read_bytes()
            if len(raw) < 32:
                return {}
            tag, payload = raw[:32], raw[32:]
            expected = hmac.new(self._key, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, expected):
                return {}
            decrypted = self._xor_cipher(payload, self._key)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return {}

    def _save_data(self, data: dict[str, str]) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except Exception:
                pass

            payload = json.dumps(data).encode("utf-8")
            encrypted = self._xor_cipher(payload, self._key)
            tag = hmac.new(self._key, encrypted, hashlib.sha256).digest()
            content = tag + encrypted

            # Write atomically with 0600 permissions
            tmp_file = self.path.with_suffix(".tmp")
            with open(tmp_file, "wb") as f:
                os.chmod(tmp_file, 0o600)
                f.write(content)
            tmp_file.replace(self.path)
            os.chmod(self.path, 0o600)
            return True
        except Exception:
            return False

    def store(self, key: str, secret: str) -> bool:
        data = self._load_data()
        data[key] = secret
        return self._save_data(data)

    def retrieve(self, key: str) -> str | None:
        data = self._load_data()
        return data.get(key)

    def delete(self, key: str) -> bool:
        data = self._load_data()
        if key in data:
            del data[key]
            return self._save_data(data)
        return False

    def exists(self, key: str) -> bool:
        return key in self._load_data()


class EnvCredentialStore(BaseCredentialStore):
    """Credential store referencing environment variables directly."""

    @staticmethod
    def _clean(key: str) -> str:
        return key[6:] if key.startswith("env://") else key

    def store(self, key: str, secret: str) -> bool:
        os.environ[self._clean(key)] = secret
        return True

    def retrieve(self, key: str) -> str | None:
        return os.environ.get(self._clean(key))

    def delete(self, key: str) -> bool:
        k = self._clean(key)
        if k in os.environ:
            del os.environ[k]
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._clean(key) in os.environ


class SecretRedactor:
    """Sanitizes text and data structures to avoid leaking credentials."""

    REDACTION_TOKEN = "***REDACTED***"
    SENSITIVE_KEY_PATTERNS = re.compile(
        r"(api[_-]?key|secret|token|password|auth|authorization|private|credential)",
        re.I,
    )
    #: Keys that match SENSITIVE_KEY_PATTERNS but hold no secret material.
    #:
    #: The whole point of the reference architecture is that a credential is
    #: addressed by an opaque URI (`secret://mission-control/openai/personal`)
    #: rather than by value, so the reference itself is safe to display -- and the
    #: Accounts view is unusable without it, because an operator cannot otherwise
    #: tell which stored secret backs which account. `authentication_type` is
    #: likewise just an enum such as `api_key` or `oauth`.
    #:
    #: Allowlisted keys are NOT returned verbatim: their values still pass through
    #: value-level redaction, so if real secret material is ever mistakenly
    #: written into one of these fields it is still caught.
    NON_SECRET_KEY_ALLOWLIST = frozenset(
        {
            "credential_reference",
            "credential_ref",
            "authentication_type",
            "auth_type",
            "auth_method",
            "login_method",
            "sso_method",
            "authMethod",
        }
    )
    SECRET_VALUE_PATTERNS = [
        re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.I),
        re.compile(r"AIza[0-9A-Za-z\-_]{35}", re.I),
        re.compile(r"gh[pousr][_-][a-zA-Z0-9]{30,}", re.I),
        re.compile(r"rnd_[a-zA-Z0-9]{20,}", re.I),
        re.compile(r"Bearer\s+([a-zA-Z0-9_\-\.]{16,})", re.I),
    ]

    TELEMETRY_KEY_PATTERNS = re.compile(
        r"(?:(?:^|_)tokens?$|_tokens|_tokens_usd|token_metrics|token_efficiency|token_count|known_tokens|total_tokens|input_tokens|output_tokens|prompt_tokens|completion_tokens|today_tokens|today_usage_tokens)",
        re.I,
    )

    def __init__(self) -> None:
        self._known_secrets: set[str] = set()

    def register_secret(self, secret: str) -> None:
        if secret and len(secret.strip()) >= 6:
            self._known_secrets.add(secret.strip())

    def redact(self, text: str) -> str:
        if not text or not isinstance(text, str):
            return text
        redacted = text
        for s in self._known_secrets:
            if s and s in redacted:
                redacted = redacted.replace(s, self.REDACTION_TOKEN)
        for pattern in self.SECRET_VALUE_PATTERNS:
            redacted = pattern.sub(self.REDACTION_TOKEN, redacted)
        return redacted

    def redact_text(self, text: str) -> str:
        """Alias for redact."""
        return self.redact(text)

    def redact_dict(self, data: Any) -> Any:
        if isinstance(data, dict):
            clean = {}
            for k, v in list(data.items()):
                key_name = str(k)
                if key_name.lower() in self.NON_SECRET_KEY_ALLOWLIST:
                    # A reference or an enum: keep it, but still scrub the value
                    # in case real secret material was written into it.
                    clean[k] = self.redact_dict(v)
                elif self.TELEMETRY_KEY_PATTERNS.search(key_name) and key_name.lower() != "token":
                    # LLM token usage counters and telemetry metrics, not secrets
                    clean[k] = self.redact_dict(v)
                elif self.SENSITIVE_KEY_PATTERNS.search(key_name):
                    clean[k] = self.REDACTION_TOKEN
                else:
                    clean[k] = self.redact_dict(v)
            return clean
        elif isinstance(data, list):
            return [self.redact_dict(item) for item in data]
        elif isinstance(data, str):
            return self.redact(data)
        return data


class CredentialManager:
    """Composite manager resolving secret references across secure backends."""

    def __init__(
        self,
        keyring_store: KeyringCredentialStore | None = None,
        file_store: EncryptedFileStore | None = None,
        env_store: EnvCredentialStore | None = None,
        primary_store: BaseCredentialStore | None = None,
        fallback_store: BaseCredentialStore | None = None,
    ) -> None:
        self.keyring = primary_store if isinstance(primary_store, KeyringCredentialStore) else (keyring_store or KeyringCredentialStore())
        self.file_store = file_store or (primary_store if isinstance(primary_store, EncryptedFileStore) else (fallback_store if isinstance(fallback_store, EncryptedFileStore) else EncryptedFileStore()))
        self.env_store = env_store or EnvCredentialStore()
        self.redactor = SecretRedactor()

    @staticmethod
    def generate_reference(provider_id: str, account_id: str) -> str:
        """Create a standardized secret reference URI."""
        return f"secret://mission-control/{provider_id}/{account_id}"

    def _parse_ref(self, ref: str) -> tuple[str, str]:
        """Parse URI into (scheme, key)."""
        ref = (ref or "").strip()
        if ref.startswith("env://"):
            return "env", ref[6:]
        if ref.startswith("secret://mission-control/"):
            return "secret", ref[25:]
        if ref.startswith("secret://"):
            return "secret", ref[9:]
        return "secret", ref

    def store(self, ref: str, secret: str) -> bool:
        """Securely store secret under the given reference."""
        if not secret:
            return False
        scheme, key = self._parse_ref(ref)
        self.redactor.register_secret(secret)
        if scheme == "env":
            return self.env_store.store(key, secret)

        # Primary: OS Keyring if available
        if self.keyring.is_available():
            if self.keyring.store(key, secret):
                # Also synchronize with encrypted file store as backup
                self.file_store.store(key, secret)
                return True

        # Fallback: Encrypted file store
        return self.file_store.store(key, secret)

    def retrieve(self, ref: str) -> str | None:
        """Retrieve the secret for a given reference URI."""
        scheme, key = self._parse_ref(ref)
        if scheme == "env":
            val = self.env_store.retrieve(key)
            if val:
                self.redactor.register_secret(val)
            return val

        # Try OS Keyring first
        if self.keyring.is_available():
            val = self.keyring.retrieve(key)
            if val:
                self.redactor.register_secret(val)
                return val

        # Fallback to encrypted file store
        val = self.file_store.retrieve(key)
        if val:
            self.redactor.register_secret(val)
        return val

    def store_credential(self, ref: str, secret: str) -> bool:
        """Alias for store() for backward compatibility."""
        return self.store(ref, secret)

    def resolve_secret(self, ref: str) -> str | None:
        """Alias for retrieve() to resolve credential references."""
        return self.retrieve(ref)

    def resolve_credential(self, ref: str) -> str | None:
        """Alias for retrieve() to resolve credential references."""
        return self.retrieve(ref)

    def delete(self, ref: str) -> bool:
        """Delete credential from all stores."""
        scheme, key = self._parse_ref(ref)
        if scheme == "env":
            return self.env_store.delete(key)
        res1 = self.keyring.delete(key) if self.keyring.is_available() else False
        res2 = self.file_store.delete(key)
        return res1 or res2

    def exists(self, ref: str) -> bool:
        """Check if credential exists without returning it."""
        scheme, key = self._parse_ref(ref)
        if scheme == "env":
            return self.env_store.exists(key)
        if self.keyring.is_available() and self.keyring.exists(key):
            return True
        return self.file_store.exists(key)

    def rotate(self, ref: str, new_secret: str) -> bool:
        """Safely replace credential value."""
        return self.store(ref, new_secret)

    def validate(self, ref: str) -> bool:
        """Validate credential presence and non-emptiness."""
        val = self.retrieve(ref)
        return bool(val and len(val.strip()) > 0)

    def get_masked_credential(self, ref: str) -> str:
        """Return masked key in format: ************abcd without leaking secret material."""
        secret = self.retrieve(ref)
        if not secret:
            return ""
        secret_str = secret.strip()
        if len(secret_str) <= 4:
            return "********"
        last_chars = secret_str[-4:]
        return f"{'*' * 12}{last_chars}"


#: Global default credential manager instance
_DEFAULT_MANAGER: CredentialManager | None = None


def get_credential_manager() -> CredentialManager:
    global _DEFAULT_MANAGER
    if _DEFAULT_MANAGER is None:
        _DEFAULT_MANAGER = CredentialManager()
    return _DEFAULT_MANAGER
