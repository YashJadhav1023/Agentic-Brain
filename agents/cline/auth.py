"""Cline Multi-Account Authentication and Isolation Management.

Provides dynamic capability discovery, directory isolation, and secure credential
management for Cline CLI (version 3.0.61+):
- Dynamically discovers installed CLI flags via `cline --version` and `cline auth --help`
- Enforces strict filesystem isolation per account using dedicated --config and --data-dir paths
- Manages API keys exclusively via CredentialManager and `secret://` URI references
- Never logs, displays, or persists raw credentials in plaintext.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers.registry.account_registry import (
    Account,
    AccountRegistry,
    AccountStatus,
    AuthenticationType,
)
from providers.registry.config import add_account_config, load_config
from providers.registry.credential_manager import get_credential_manager
from providers.registry.lifecycle import (
    AccountLifecycleState,
    AccountLifecycleStateMachine,
    AuthState,
    HealthState,
    ProcessState,
)

logger = logging.getLogger(__name__)

DEFAULT_CLINE_ROOT = Path.home() / ".mission-control" / "cline"


@dataclass
class ClineCapabilities:
    """Discovered capabilities and supported flags for installed Cline CLI."""
    version: str
    has_auth_command: bool
    supports_config_flag: bool
    supports_data_dir_flag: bool
    auth_flags: list[str] = field(default_factory=list)
    executable_path: str = ""
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "has_auth_command": self.has_auth_command,
            "supports_config_flag": self.supports_config_flag,
            "supports_data_dir_flag": self.supports_data_dir_flag,
            "auth_flags": self.auth_flags,
            "executable_path": self.executable_path,
            "discovered_at": self.discovered_at,
        }


_GLOBAL_CLINE_CAPS: dict[str, ClineCapabilities] = {}


class ClineAuthManager:
    """Manages Cline CLI capability discovery, profile isolation, and authentication."""

    def __init__(
        self,
        executable: str = "cline",
        base_dir: Path | None = None,
        registry: AccountRegistry | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.executable = executable
        self.base_dir = Path(base_dir).expanduser() if base_dir else DEFAULT_CLINE_ROOT
        self.registry = registry
        self.config_path = config_path
        self._capabilities_cache: ClineCapabilities | None = None

    def resolve_executable(self) -> str | None:
        """Find executable on PATH or direct path."""
        found = shutil.which(self.executable)
        if found:
            return found
        p = Path(self.executable).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None

    def discover_capabilities(self, force_refresh: bool = False) -> ClineCapabilities:
        """Dynamically inspect installed cline binary via --version and --help."""
        exe = self.resolve_executable() or ""
        if not force_refresh:
            if self._capabilities_cache:
                return self._capabilities_cache
            if exe in _GLOBAL_CLINE_CAPS:
                self._capabilities_cache = _GLOBAL_CLINE_CAPS[exe]
                return self._capabilities_cache

        if not exe:
            cap = ClineCapabilities(
                version="not_installed",
                has_auth_command=False,
                supports_config_flag=False,
                supports_data_dir_flag=False,
                auth_flags=[],
                executable_path="",
            )
            self._capabilities_cache = cap
            _GLOBAL_CLINE_CAPS[""] = cap
            return cap

        version_str = "unknown"
        try:
            res_v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
            if res_v.returncode == 0 and res_v.stdout.strip():
                version_str = res_v.stdout.strip().splitlines()[0].strip()
        except Exception as e:
            logger.warning(f"Error querying cline --version: {e}")

        # Probe cline auth --help
        has_auth = False
        auth_flags: list[str] = []
        try:
            res_auth = subprocess.run([exe, "auth", "--help"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
            if res_auth.returncode == 0:
                has_auth = True
                help_text = res_auth.stdout
                for flag in re.findall(r"(--[a-zA-Z0-9\-]+|-[a-zA-Z0-9])", help_text):
                    if flag not in auth_flags:
                        auth_flags.append(flag)
        except Exception as e:
            logger.warning(f"Error querying cline auth --help: {e}")

        # Probe global flags
        supports_config = "--config" in auth_flags
        supports_data_dir = "--data-dir" in auth_flags

        if not (supports_config and supports_data_dir):
            try:
                res_main = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
                if res_main.returncode == 0:
                    main_help = res_main.stdout
                    if "--config" in main_help:
                        supports_config = True
                    if "--data-dir" in main_help:
                        supports_data_dir = True
            except Exception:
                pass

        # Fallback: if discovery times out or fails to detect -k, provide default fallback flags
        fallback_flags = ["-k", "-m", "-b", "--config", "--data-dir", "-p"]
        if "-k" not in auth_flags:
            logger.info("Cline auth -k flag not detected; applying default fallback flags")
            has_auth = True
            for flag in fallback_flags:
                if flag not in auth_flags:
                    auth_flags.append(flag)
            supports_config = True
            supports_data_dir = True

        cap = ClineCapabilities(
            version=version_str,
            has_auth_command=has_auth,
            supports_config_flag=supports_config,
            supports_data_dir_flag=supports_data_dir,
            auth_flags=auth_flags,
            executable_path=exe,
        )
        self._capabilities_cache = cap
        _GLOBAL_CLINE_CAPS[exe] = cap
        return cap

    def setup_account_isolation(self, account_id: str) -> tuple[Path, Path]:
        """Create dedicated, isolated config and data directories for a Cline account."""
        clean_id = account_id.strip()
        if not clean_id:
            raise ValueError("account_id must not be empty")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", clean_id):
            raise ValueError(
                f"Invalid account_id '{clean_id}': must contain only alphanumeric characters, underscores, and dashes"
            )

        account_root = (self.base_dir / clean_id).resolve()
        base_dir_resolved = self.base_dir.resolve()
        try:
            account_root.relative_to(base_dir_resolved)
        except ValueError:
            raise ValueError(f"Account directory '{account_root}' escapes base directory '{base_dir_resolved}'")

        if account_root == base_dir_resolved:
            raise ValueError(f"Account directory cannot be base directory itself: {account_root}")

        config_dir = account_root / "config"
        data_dir = account_root / "data"

        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        try:
            config_dir.chmod(0o700)
            data_dir.chmod(0o700)
            account_root.chmod(0o700)
        except OSError:
            pass

        return config_dir, data_dir

    def store_credential(self, account_id: str, api_key: str) -> str:
        """Store API key in CredentialManager and return safe secret reference URI."""
        clean_id = account_id.strip()
        if not clean_id:
            raise ValueError("account_id must not be empty")
        if not re.match(r"^[a-zA-Z0-9_\-]+$", clean_id):
            raise ValueError(
                f"Invalid account_id '{clean_id}': must contain only alphanumeric characters, underscores, and dashes"
            )
        if not api_key:
            raise ValueError("api_key must not be empty")
        ref = f"secret://mission-control/cline/{clean_id}/api_key"
        cm = get_credential_manager()
        cm.store(ref, api_key)
        return ref

    def authenticate(
        self,
        account_id: str,
        provider: str,
        api_key: str | None = None,
        credential_reference: str | None = None,
        model_id: str | None = None,
        base_url: str | None = None,
        execute_cli: bool = True,
    ) -> dict[str, Any]:
        """Configure credentials and optionally run `cline auth` for the isolated profile."""
        config_dir, data_dir = self.setup_account_isolation(account_id)
        cm = get_credential_manager()

        if api_key:
            cred_ref = self.store_credential(account_id, api_key)
            secret_value = api_key
        elif credential_reference:
            cred_ref = credential_reference
            secret_value = cm.retrieve(credential_reference)
            if not secret_value:
                return {
                    "success": False,
                    "error": f"Credential reference '{credential_reference}' could not be resolved",
                    "account_id": account_id,
                }
        else:
            return {
                "success": False,
                "error": "Either api_key or credential_reference must be supplied",
                "account_id": account_id,
            }

        caps = self.discover_capabilities()
        exe = caps.executable_path or self.resolve_executable()

        if not exe:
            return {
                "success": False,
                "error": "Cline CLI executable not found on system",
                "account_id": account_id,
                "credential_reference": cred_ref,
            }

        # Build command safely
        cmd = [exe, "auth", "-p", provider]
        if "-k" in caps.auth_flags or "--apikey" in caps.auth_flags:
            cmd.extend(["-k", secret_value])
        if model_id and ("-m" in caps.auth_flags or "--modelid" in caps.auth_flags):
            cmd.extend(["-m", model_id])
        if base_url and ("-b" in caps.auth_flags or "--baseurl" in caps.auth_flags):
            cmd.extend(["-b", base_url])
        if caps.supports_config_flag:
            cmd.extend(["--config", str(config_dir)])
        if caps.supports_data_dir_flag:
            cmd.extend(["--data-dir", str(data_dir)])

        # Redacted command representation for auditing / return payloads
        redacted_cmd = [
            "<API_KEY_REDACTED>" if tok == secret_value else tok
            for tok in cmd
        ]

        if execute_cli:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                success = proc.returncode == 0
                output = proc.stdout if success else (proc.stderr or proc.stdout)
                return {
                    "success": success,
                    "account_id": account_id,
                    "provider": provider,
                    "credential_reference": cred_ref,
                    "masked_key": cm.get_masked_credential(cred_ref),
                    "config_dir": str(config_dir),
                    "data_dir": str(data_dir),
                    "command_redacted": " ".join(redacted_cmd),
                    "output": output.strip(),
                    "exit_code": proc.returncode,
                }
            except Exception as e:
                return {
                    "success": False,
                    "account_id": account_id,
                    "provider": provider,
                    "credential_reference": cred_ref,
                    "error": str(e),
                }

        return {
            "success": True,
            "account_id": account_id,
            "provider": provider,
            "credential_reference": cred_ref,
            "masked_key": cm.get_masked_credential(cred_ref),
            "config_dir": str(config_dir),
            "data_dir": str(data_dir),
            "command_redacted": " ".join(redacted_cmd),
        }

    def validate_account(self, account_id: str) -> tuple[bool, str]:
        """Validate that a Cline account's directories and credentials exist."""
        clean_id = account_id.strip()
        if not clean_id or not re.match(r"^[a-zA-Z0-9_\-]+$", clean_id):
            return False, f"Invalid account_id: must contain only alphanumeric characters, underscores, and dashes"
        account_root = (self.base_dir / clean_id).resolve()
        config_dir = account_root / "config"
        data_dir = account_root / "data"

        if not config_dir.is_dir():
            return False, f"Config directory does not exist: {config_dir}"
        if not data_dir.is_dir():
            return False, f"Data directory does not exist: {data_dir}"

        ref = f"secret://mission-control/cline/{clean_id}/api_key"
        cm = get_credential_manager()
        if not cm.exists(ref):
            return False, f"Credential for account '{clean_id}' not found in CredentialManager"

        return True, f"Account '{clean_id}' configured and credentials verified"

    def register_account(
        self,
        account_id: str,
        credential_reference: str,
        display_name: str | None = None,
        priority: int = 10,
        models: list[str] | None = None,
        capabilities: list[str] | None = None,
        persist_config: bool = True,
    ) -> Account:
        """Register configured Cline account in AccountRegistry and optionally persist."""
        config_dir, data_dir = self.setup_account_isolation(account_id)
        acct_models = models or ["auto"]
        acct_capabilities = capabilities or [
            "editor_refactoring",
            "frontend_styling",
            "component_refactoring",
            "code_review",
        ]

        account = Account(
            id=account_id,
            provider_id="cline",
            account_name=account_id,
            display_name=display_name or f"Cline ({account_id})",
            account_type="agent",
            authentication_type=AuthenticationType.API_KEY,
            credential_reference=credential_reference,
            status=AccountStatus.ONLINE,
            enabled=True,
            priority=priority,
            models=acct_models,
            capabilities=acct_capabilities,
            metadata={
                "config_dir": str(config_dir),
                "data_dir": str(data_dir),
            },
            lifecycle_state=AccountLifecycleState.ONLINE,
            auth_state=AuthState.AUTHENTICATED,
            health_state=HealthState.HEALTHY,
            process_state=ProcessState.IDLE,
        )

        if self.registry:
            self.registry.register_account(account)

        if persist_config and self.config_path:
            account_conf = {
                "account_id": account.account_name,
                "agent_id": account.id,
                "display_name": account.display_name,
                "priority": account.priority,
                "enabled": True,
                "models": acct_models,
                "default_model": acct_models[0] if acct_models else "auto",
                "capabilities": acct_capabilities,
                "config_dir": str(config_dir),
                "data_dir": str(data_dir),
                "credential_reference": credential_reference,
            }
            try:
                add_account_config("cline", account.id, account_conf, str(self.config_path))
            except Exception as e:
                logger.warning(f"Could not persist Cline account config: {e}")

        return account
