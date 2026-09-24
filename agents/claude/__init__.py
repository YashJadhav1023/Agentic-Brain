"""Claude Code provider: headless execution across isolated subscription accounts."""
from agents.claude.adapter import ClaudeAdapter, classify_failure
from agents.claude.auth import (
    ClaudeAuthMode,
    ProfileStatus,
    ensure_profile_dir,
    inspect_profile,
    login_command,
    logout_command,
    resolve_config_dir,
)

__all__ = [
    "ClaudeAdapter",
    "ClaudeAuthMode",
    "ProfileStatus",
    "classify_failure",
    "ensure_profile_dir",
    "inspect_profile",
    "login_command",
    "logout_command",
    "resolve_config_dir",
]
