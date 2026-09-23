"""Provider configuration loader.

`config/providers.json` is the single source of truth for provider, account,
execution and model configuration. Every adapter reads its command, profile,
capabilities and model catalogue from here, so no CLI flag, profile path or
model id is hard-coded in Python.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from agents.base.adapter import Capability

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "providers.json"

#: Environment variable overriding which configuration file is read and written.
#:
#: This exists so a test, a sandbox, or an alternate profile can operate on a
#: throwaway copy instead of the canonical file. Anything that adds, removes or
#: disables a provider or account writes back to whichever path resolves here,
#: so without an override a lifecycle test would mutate the user's real
#: configuration and make the suite order-dependent.
CONFIG_PATH_ENV_VAR = "BRAIN_PROVIDERS_CONFIG"


def resolve_config_path() -> Path:
    """Return the configuration file to use, honouring the env override."""
    override = os.environ.get(CONFIG_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return CONFIG_PATH


@dataclass(frozen=True)
class AccountConfigView:
    """Read-only view of one account entry, provider-neutral."""

    agent_id: str
    account_id: str
    provider_id: str
    enabled: bool
    command: str
    capabilities: frozenset[Capability]
    models: tuple[str, ...]
    default_model: str
    raw: dict[str, Any]


@lru_cache(maxsize=4)
def load_config(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else resolve_config_path()
    if not target.is_file():
        raise FileNotFoundError(f"Provider configuration not found: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def _to_capabilities(values: list[str]) -> frozenset[Capability]:
    resolved = set()
    for value in values:
        try:
            resolved.add(Capability(value))
        except ValueError:
            # An unknown capability in config must not crash the registry; it is
            # simply not routable until the Capability enum learns about it.
            continue
    return frozenset(resolved)


def get_accounts(provider_id: str, path: str | None = None) -> list[AccountConfigView]:
    provider = load_config(path).get("providers", {}).get(provider_id, {})
    if not provider:
        return []
    provider_command = provider.get("command", provider_id)
    views: list[AccountConfigView] = []
    for agent_key, account in provider.get("accounts", {}).items():
        execution = account.get("execution", {})
        views.append(
            AccountConfigView(
                agent_id=account.get("agent_id", agent_key),
                account_id=account.get("account_id", "cli"),
                provider_id=provider_id,
                enabled=bool(account.get("enabled", True)) and bool(provider.get("enabled", True)),
                command=execution.get("command") or provider_command,
                capabilities=_to_capabilities(account.get("capabilities", [])),
                models=tuple(account.get("models", ())),
                default_model=account.get("default_model", "auto"),
                raw=account,
            )
        )
    return views


def get_account(provider_id: str, agent_id: str, path: str | None = None) -> AccountConfigView | None:
    for view in get_accounts(provider_id, path):
        if view.agent_id == agent_id:
            return view
    return None


def get_provider_meta(provider_id: str, path: str | None = None) -> dict[str, Any]:
    return load_config(path).get("providers", {}).get(provider_id, {})


def max_concurrent_agents(path: str | None = None) -> int:
    """Bounded concurrency for this host, from config."""
    return int(load_config(path).get("concurrency", {}).get("max_concurrent_agents", 3))


def save_config(data: dict[str, Any], path: str | None = None) -> None:
    """Safely persist configuration data without exposing secrets."""
    target = Path(path) if path else resolve_config_path()
    # A unique temp name per write: a fixed "providers.tmp" let two concurrent
    # savers (dashboard threads, CLI) interleave into one temp file and publish
    # a torn providers.json via replace().
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    finally:
        load_config.cache_clear()


def add_account_config(
    provider_id: str,
    account_id: str,
    account_data: dict[str, Any],
    path: str | None = None,
) -> bool:
    """Add or update an account entry in providers.json."""
    data = load_config(path)
    providers = data.setdefault("providers", {})
    provider = providers.setdefault(provider_id, {"id": provider_id, "name": provider_id.title(), "enabled": True})
    accounts = provider.setdefault("accounts", {})
    accounts[account_id] = account_data
    save_config(data, path)
    return True


def remove_account_config(
    provider_id: str, account_id: str, path: str | None = None
) -> bool:
    """Remove an account entry from providers.json."""
    data = load_config(path)
    accounts = data.get("providers", {}).get(provider_id, {}).get("accounts", {})
    if account_id in accounts:
        del accounts[account_id]
        save_config(data, path)
        return True
    return False


def set_account_enabled_config(
    provider_id: str, account_id: str, enabled: bool, path: str | None = None
) -> bool:
    """Toggle the enabled status of an account."""
    data = load_config(path)
    account = data.get("providers", {}).get(provider_id, {}).get("accounts", {}).get(account_id)
    if account:
        account["enabled"] = enabled
        save_config(data, path)
        return True
    return False


def add_provider_config(
    provider_id: str, provider_data: dict[str, Any], path: str | None = None
) -> bool:
    """Add or update a provider entry in providers.json."""
    data = load_config(path)
    providers = data.setdefault("providers", {})
    providers[provider_id] = provider_data
    save_config(data, path)
    return True
