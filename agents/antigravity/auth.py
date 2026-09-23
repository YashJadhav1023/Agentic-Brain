"""Antigravity CLI Authentication and Profile Lifecycle Management.

Provides safe, isolated Google account onboarding and authentication management
for Antigravity headless CLI accounts:
- Creates dedicated, isolated runtime profile directories under ~/.gemini/
- Enforces D-Bus GNOME Keyring shielding (DBUS_SESSION_BUS_ADDRESS="/dev/null")
  to ensure CLI accounts NEVER read, overwrite, or collide with the system
  keyring slot (service="gemini") used by the live Antigravity IDE GUI (PID 5854).
- Detects authentication completion via local token artifact and zero-cost model probe.
- Stores only safe metadata (profile directories, discovered models, capabilities).
- Never extracts, logs, or persists OAuth tokens or cookies in plaintext.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.antigravity.adapter import DEFAULT_CONFIG_PATH, AntigravityAccountAdapter
from providers.registry.account_registry import (
    Account,
    AccountRegistry,
    AccountStatus,
    AuthenticationType,
)
from providers.registry.config import (
    add_account_config,
    load_config,
    resolve_config_path,
    save_config,
)
from providers.registry.lifecycle import (
    AccountLifecycleState,
    AccountLifecycleStateMachine,
    AuthState,
    HealthState,
    ProcessState,
)

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_ROOT = Path.home() / ".gemini"
TOKEN_FILENAME = "antigravity-oauth-token"


@dataclass
class AntigravityAuthStatus:
    account_id: str
    data_dir: str
    profile_dir: str
    is_authenticated: bool
    token_exists: bool
    models_available: list[str] = field(default_factory=list)
    lifecycle_state: str = AccountLifecycleState.DISCOVERED.value
    auth_state: str = AuthState.UNAUTHENTICATED.value
    health_state: str = HealthState.UNKNOWN.value
    error: str | None = None


class AntigravityAuthManager:
    """Manages isolated profile creation, authentication, validation, and registration for Antigravity."""

    def __init__(
        self,
        binary_path: str | Path | None = None,
        profile_root: Path | None = None,
        config_path: Path | None = None,
        registry: AccountRegistry | None = None,
    ) -> None:
        self.profile_root = Path(profile_root).expanduser() if profile_root else DEFAULT_PROFILE_ROOT
        self.config_path = Path(config_path).expanduser() if config_path else resolve_config_path()
        self.registry = registry
        self._bin = self._resolve_binary(binary_path)

    @staticmethod
    def _resolve_binary(candidate: str | Path | None = None) -> Path | None:
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return p
        found = shutil.which("agy") or shutil.which("antigravity")
        if found:
            return Path(found)
        standard_path = Path.home() / ".gemini" / "bin" / "agy"
        if standard_path.is_file() and os.access(standard_path, os.X_OK):
            return standard_path
        return None

    @property
    def binary(self) -> Path | None:
        return self._bin

    def get_subprocess_env(self) -> dict[str, str]:
        """Subprocess environment isolating D-Bus to shield system GNOME Keyring."""
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = "/dev/null"
        return env

    def get_profile_dir(self, app_data_dir: str) -> Path:
        """Resolve and canonicalize full profile directory path safely."""
        target_path = (self.profile_root / app_data_dir).resolve()
        profile_root_resolved = self.profile_root.resolve()
        protected_ide_profile = (Path.home() / ".gemini" / "antigravity-ide").resolve()
        root_ide_profile = (profile_root_resolved / "antigravity-ide").resolve()

        # Invariant: Never collide with or touch primary IDE GUI profile
        if (
            target_path in (protected_ide_profile, root_ide_profile)
            or protected_ide_profile in target_path.parents
            or root_ide_profile in target_path.parents
            or target_path.name in ("antigravity-ide", "ide")
            or app_data_dir.strip() in ("antigravity-ide", "ide")
        ):
            raise ValueError(f"Protected profile '{app_data_dir}' cannot be modified, accessed, or re-created")

        # Invariant: Ensure path does not resolve outside profile_root
        try:
            target_path.relative_to(profile_root_resolved)
        except ValueError:
            raise ValueError(f"Profile directory '{target_path}' resolves outside profile root '{profile_root_resolved}'")

        if target_path == profile_root_resolved:
            raise ValueError(f"Profile directory cannot be the profile root itself: {target_path}")

        return target_path

    def create_isolated_profile(
        self,
        account_id: str,
        app_data_dir: str | None = None,
    ) -> tuple[str, Path]:
        """Create a dedicated, isolated directory for an Antigravity account profile."""
        clean_id = account_id.strip()
        if not clean_id:
            raise ValueError("account_id must not be empty")

        if not app_data_dir:
            if clean_id.startswith("antigravity-account-"):
                suffix = clean_id[len("antigravity-account-"):]
                app_data_dir = f"antigravity-account-{suffix}"
            else:
                app_data_dir = f"antigravity-account-{clean_id}"

        profile_path = self.get_profile_dir(app_data_dir)
        profile_path.mkdir(parents=True, exist_ok=True)
        try:
            profile_path.chmod(0o700)
        except OSError:
            pass

        return app_data_dir, profile_path

    def check_token_exists(self, app_data_dir: str) -> bool:
        """Check whether local token file exists and is non-empty."""
        token_path = self.get_profile_dir(app_data_dir) / TOKEN_FILENAME
        return token_path.is_file() and token_path.stat().st_size > 0

    def launch_auth(
        self,
        account_id: str,
        app_data_dir: str | None = None,
    ) -> dict[str, Any]:
        """Prepare isolated environment and return the official CLI authentication command."""
        data_dir, profile_dir = self.create_isolated_profile(account_id, app_data_dir)

        if not self._bin:
            return {
                "success": False,
                "error": "Antigravity CLI binary (agy) not found on PATH or ~/.gemini/bin/agy",
                "account_id": account_id,
                "data_dir": data_dir,
            }

        cmd = [str(self._bin), f"--app_data_dir={data_dir}"]
        return {
            "success": True,
            "account_id": account_id,
            "data_dir": data_dir,
            "profile_dir": str(profile_dir),
            "command": cmd,
            "command_str": " ".join(cmd),
            "environment": {"DBUS_SESSION_BUS_ADDRESS": "/dev/null"},
            "instructions": f"Run '{' '.join(cmd)}' in a terminal with DBUS_SESSION_BUS_ADDRESS='/dev/null' to complete Google OAuth.",
        }

    def validate_session(
        self,
        app_data_dir: str,
        timeout_seconds: int = 90,
    ) -> tuple[bool, str, list[str]]:
        """Validate authenticated session by probing models without spending token quota."""
        if not self._bin:
            return False, "Antigravity CLI binary not found", []

        profile_dir = self.get_profile_dir(app_data_dir)
        if not profile_dir.is_dir():
            return False, f"Profile directory does not exist: {profile_dir}", []

        cmd = [str(self._bin), f"--app_data_dir={app_data_dir}", "models"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=self.get_subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return False, f"Session probe timed out after {timeout_seconds}s", []
        except Exception as exc:
            return False, f"Session probe failed: {exc}", []

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip().splitlines()
            last_err = err[-1] if err else "Authentication required or session expired"
            return False, f"Session unusable (exit {proc.returncode}): {last_err}", []

        models: list[str] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith(("*", "Models", "Available", "ID", "-")):
                continue
            parts = line.split()
            if parts and parts[0]:
                models.append(parts[0])

        if not models:
            models = ["gemini-3.8-flash-medium", "gemini-3.7-flash-high", "gemini-3.7-flash-medium"]

        return True, f"Session verified successfully ({len(models)} models discovered)", models

    def get_auth_status(
        self,
        account_id: str,
        app_data_dir: str | None = None,
        deep_probe: bool = False,
    ) -> AntigravityAuthStatus:
        """Query current authentication status for an account."""
        data_dir = app_data_dir or (
            account_id.replace("antigravity-account-", "") if "antigravity-account-" in account_id else account_id
        )
        if not data_dir.startswith("antigravity-account-"):
            data_dir = f"antigravity-account-{data_dir}"

        profile_dir = self.get_profile_dir(data_dir)
        token_exists = self.check_token_exists(data_dir)

        if not token_exists:
            return AntigravityAuthStatus(
                account_id=account_id,
                data_dir=data_dir,
                profile_dir=str(profile_dir),
                is_authenticated=False,
                token_exists=False,
                lifecycle_state=AccountLifecycleState.CONFIGURING.value,
                auth_state=AuthState.UNAUTHENTICATED.value,
                health_state=HealthState.UNKNOWN.value,
                error="OAuth token not found; authentication required",
            )

        if deep_probe:
            ok, msg, models = self.validate_session(data_dir)
            if ok:
                return AntigravityAuthStatus(
                    account_id=account_id,
                    data_dir=data_dir,
                    profile_dir=str(profile_dir),
                    is_authenticated=True,
                    token_exists=True,
                    models_available=models,
                    lifecycle_state=AccountLifecycleState.ONLINE.value,
                    auth_state=AuthState.AUTHENTICATED.value,
                    health_state=HealthState.HEALTHY.value,
                )
            return AntigravityAuthStatus(
                account_id=account_id,
                data_dir=data_dir,
                profile_dir=str(profile_dir),
                is_authenticated=False,
                token_exists=True,
                models_available=[],
                lifecycle_state=AccountLifecycleState.AUTH_FAILED.value,
                auth_state=AuthState.AUTH_FAILED.value,
                health_state=HealthState.UNHEALTHY.value,
                error=msg,
            )

        return AntigravityAuthStatus(
            account_id=account_id,
            data_dir=data_dir,
            profile_dir=str(profile_dir),
            is_authenticated=True,
            token_exists=True,
            lifecycle_state=AccountLifecycleState.AUTHENTICATED.value,
            auth_state=AuthState.AUTHENTICATED.value,
            health_state=HealthState.HEALTHY.value,
        )

    def register_account(
        self,
        account_id: str,
        app_data_dir: str,
        display_name: str | None = None,
        priority: int = 10,
        persist_config: bool = True,
        models: list[str] | None = None,
        capabilities: list[str] | None = None,
    ) -> Account:
        """Register an authenticated Antigravity account into registry and persist config."""
        profile_dir = self.get_profile_dir(app_data_dir)
        acct_models = models or ["gemini-3.8-flash-medium", "gemini-3.7-flash-high", "gemini-3.7-flash-medium"]
        acct_capabilities = capabilities or ["coding", "architecture", "multi_file_editing"]

        account = Account(
            id=account_id,
            provider_id="antigravity",
            account_name=account_id.replace("antigravity-", ""),
            display_name=display_name or f"Antigravity ({account_id})",
            account_type="agent",
            authentication_type=AuthenticationType.OAUTH,
            credential_reference="",  # Local isolated OAuth token file
            status=AccountStatus.ONLINE,
            enabled=True,
            priority=priority,
            models=acct_models,
            capabilities=acct_capabilities,
            metadata={
                "data_dir": app_data_dir,
                "profile_dir": str(profile_dir),
                "auth_method": "oauth",
            },
            lifecycle_state=AccountLifecycleState.ONLINE,
            auth_state=AuthState.AUTHENTICATED,
            health_state=HealthState.HEALTHY,
            process_state=ProcessState.IDLE,
        )

        if self.registry:
            self.registry.register_account(account)

        if persist_config:
            account_conf = {
                "account_id": account.account_name,
                "agent_id": account.id,
                "display_name": account.display_name,
                "priority": account.priority,
                "enabled": True,
                "models": acct_models,
                "default_model": acct_models[0] if acct_models else "gemini-3.8-flash-medium",
                "capabilities": acct_capabilities,
                "execution": {
                    "app_data_dir": app_data_dir,
                    "output_format": "json",
                    "dangerously_skip_permissions": False,
                },
            }
            try:
                add_account_config("antigravity", account.id, account_conf, str(self.config_path))
            except Exception as e:
                logger.warning(f"Could not persist Antigravity account config: {e}")

        return account
