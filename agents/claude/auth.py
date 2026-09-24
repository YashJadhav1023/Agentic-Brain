"""Claude Code account authentication, without ever holding a credential.

Three authentication modes are supported, and only one of them involves a
secret this project can see:

``subscription``
    The account is a Claude Pro/Max seat that the human logged in to with the
    official ``claude`` CLI. The credential lives in that account's own
    ``CLAUDE_CONFIG_DIR`` and is owned, refreshed and revoked by the official
    CLI. The brain reads *metadata only* (plan name, expiry) so it can report
    health, and never reads, copies, logs or transports the token itself.

``oauth_token``
    A long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` the user minted themselves with
    ``claude setup-token``. Stored in the CredentialManager and injected as an
    environment variable for the duration of one subprocess.

``api_key``
    A standard ``sk-ant-…`` API key, billed per token. Also stored in the
    CredentialManager.

Why there is no ``brain claude login`` that performs its own OAuth flow: minting
a Claude subscription token from outside the official client requires presenting
Anthropic's first-party OAuth client id, i.e. impersonating Claude Code. This
project refuses to ship that. Logging in is delegated to the real CLI, which is
both the supported path and the one that cannot get a contributor's account
banned. See ``docs/CLAUDE_ACCOUNTS.md``.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

#: Filename the official CLI uses inside a config dir for its OAuth credential.
CREDENTIALS_FILENAME = ".credentials.json"

#: Environment variable the official CLI reads to relocate its whole profile.
#: This is what makes one Claude account per brain account possible without the
#: accounts being able to see each other's session.
CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"

#: A credential inside this many seconds of expiry is reported as expiring so an
#: operator can refresh before a long task dies halfway through. The official CLI
#: refreshes on its own; this is a warning, never an error.
EXPIRY_WARNING_SECONDS = 15 * 60


class ClaudeAuthMode(str, Enum):
    """How one Claude account proves who it is."""

    #: Pro/Max seat logged in via the official CLI; credential owned by that CLI.
    SUBSCRIPTION = "subscription"
    #: Long-lived token from `claude setup-token`, held in the CredentialManager.
    OAUTH_TOKEN = "oauth_token"
    #: Pay-per-token `sk-ant-…` key, held in the CredentialManager.
    API_KEY = "api_key"

    @classmethod
    def parse(cls, value: str | None) -> ClaudeAuthMode:
        """Resolve a human or config spelling, defaulting to subscription.

        Subscription is the default because it is the mode that requires no
        secret to be handed to this project at all.
        """
        if not value:
            return cls.SUBSCRIPTION
        normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "subscription": cls.SUBSCRIPTION,
            "sub": cls.SUBSCRIPTION,
            "max": cls.SUBSCRIPTION,
            "pro": cls.SUBSCRIPTION,
            "claude_max": cls.SUBSCRIPTION,
            "session": cls.SUBSCRIPTION,
            "oauth_token": cls.OAUTH_TOKEN,
            "oauth": cls.OAUTH_TOKEN,
            "setup_token": cls.OAUTH_TOKEN,
            "token": cls.OAUTH_TOKEN,
            "api_key": cls.API_KEY,
            "apikey": cls.API_KEY,
            "key": cls.API_KEY,
        }
        return aliases.get(normalized, cls.SUBSCRIPTION)


@dataclass(frozen=True)
class ProfileStatus:
    """Metadata about one account's login state.

    Deliberately contains no credential material: there is no field here that
    could carry an access token, a refresh token, or an API key, so this object
    is safe to serialize into telemetry, the dashboard and the event log.
    """

    config_dir: Path
    exists: bool
    logged_in: bool
    reason: str
    plan: str | None = None
    rate_limit_tier: str | None = None
    expires_at_epoch: int | None = None
    seconds_until_expiry: int | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)
    world_readable: bool = False

    @property
    def expiring_soon(self) -> bool:
        """True when a valid credential is close enough to expiry to mention."""
        if not self.logged_in or self.seconds_until_expiry is None:
            return False
        return self.seconds_until_expiry <= EXPIRY_WARNING_SECONDS

    def to_dict(self) -> dict[str, Any]:
        """Serializable form. Contains metadata only, never a secret."""
        return {
            "config_dir": str(self.config_dir),
            "exists": self.exists,
            "logged_in": self.logged_in,
            "reason": self.reason,
            "plan": self.plan,
            "rate_limit_tier": self.rate_limit_tier,
            "expires_at_epoch": self.expires_at_epoch,
            "seconds_until_expiry": self.seconds_until_expiry,
            "expiring_soon": self.expiring_soon,
            "scopes": list(self.scopes),
            "world_readable": self.world_readable,
        }


def resolve_config_dir(config_dir: str | Path | None) -> Path:
    """Expand an account's profile directory, defaulting to the shared one.

    ``None`` means "whatever the human's own ``claude`` uses", i.e. ``~/.claude``.
    That makes the first account zero-setup: if you are already logged in, the
    brain can use that session without you re-authenticating.
    """
    if config_dir is None or str(config_dir).strip() == "":
        env_override = os.environ.get(CONFIG_DIR_ENV_VAR, "").strip()
        if env_override:
            return Path(env_override).expanduser()
        return Path.home() / ".claude"
    return Path(str(config_dir)).expanduser()


def ensure_profile_dir(config_dir: str | Path) -> Path:
    """Create an account's profile directory, owner-only.

    A Claude profile holds an OAuth credential, so the directory is created 0700
    and an existing directory that is group- or world-accessible is tightened
    rather than silently accepted. Raises if it cannot be made private, because
    continuing would mean writing a credential somewhere other local users can
    read it.
    """
    target = Path(str(config_dir)).expanduser()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = stat.S_IMODE(target.stat().st_mode)
    if mode & 0o077:
        target.chmod(0o700)
        if stat.S_IMODE(target.stat().st_mode) & 0o077:
            raise PermissionError(
                f"{target} is accessible by other users and could not be made private"
            )
    return target


def _credential_is_world_readable(path: Path) -> bool:
    try:
        return bool(stat.S_IMODE(path.stat().st_mode) & 0o077)
    except OSError:
        return False


def inspect_profile(config_dir: str | Path | None) -> ProfileStatus:
    """Report whether one account is logged in, reading metadata only.

    The credential file is parsed because that is the only way to know whether a
    session is live and which plan it is on, but the two token fields are never
    bound to a variable, returned, or logged. A malformed or unreadable file is
    reported as "not logged in" rather than raised: an operator adding a second
    account should get a usable status line, not a traceback.
    """
    target = resolve_config_dir(config_dir)
    if not target.exists():
        return ProfileStatus(
            config_dir=target,
            exists=False,
            logged_in=False,
            reason=f"profile directory does not exist: {target}",
        )

    credentials = target / CREDENTIALS_FILENAME
    if not credentials.is_file():
        return ProfileStatus(
            config_dir=target,
            exists=True,
            logged_in=False,
            reason=f"not logged in; no {CREDENTIALS_FILENAME} in {target}",
        )

    world_readable = _credential_is_world_readable(credentials)
    try:
        payload = json.loads(credentials.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return ProfileStatus(
            config_dir=target,
            exists=True,
            logged_in=False,
            reason=f"credential file unreadable ({exc.__class__.__name__})",
            world_readable=world_readable,
        )

    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if not isinstance(oauth, dict):
        return ProfileStatus(
            config_dir=target,
            exists=True,
            logged_in=False,
            reason="credential file has no claudeAiOauth section (API-key-only profile?)",
            world_readable=world_readable,
        )

    # `expiresAt` is milliseconds since epoch in every observed version. A value
    # small enough to be seconds is treated as seconds so a format change cannot
    # make a live session look decades expired.
    raw_expiry = oauth.get("expiresAt")
    expires_at: int | None = None
    if isinstance(raw_expiry, (int, float)) and raw_expiry > 0:
        expires_at = int(raw_expiry / 1000) if raw_expiry > 10**11 else int(raw_expiry)

    remaining = int(expires_at - time.time()) if expires_at is not None else None
    # Absent expiry is treated as live: the official CLI is the authority on
    # refresh, and refusing to run because a field is missing would be a
    # self-inflicted outage.
    live = remaining is None or remaining > 0

    scopes_raw = oauth.get("scopes")
    scopes = tuple(str(s) for s in scopes_raw) if isinstance(scopes_raw, list) else ()

    plan = oauth.get("subscriptionType")
    reason = (
        f"logged in ({plan or 'unknown plan'})"
        if live
        else f"credential expired {abs(remaining or 0)}s ago; run the login command again"
    )
    return ProfileStatus(
        config_dir=target,
        exists=True,
        logged_in=live,
        reason=reason,
        plan=str(plan) if plan else None,
        rate_limit_tier=str(oauth["rateLimitTier"]) if oauth.get("rateLimitTier") else None,
        expires_at_epoch=expires_at,
        seconds_until_expiry=remaining,
        scopes=scopes,
        world_readable=world_readable,
    )


def login_command(config_dir: str | Path | None, executable: str = "claude") -> str:
    """The exact command a human runs to log this account in.

    Interactive by design. The brain prints this instead of driving the flow,
    because the OAuth exchange belongs to the official client.
    """
    target = resolve_config_dir(config_dir)
    return f'{CONFIG_DIR_ENV_VAR}="{target}" {executable} /login'


def logout_command(config_dir: str | Path | None, executable: str = "claude") -> str:
    """The command that revokes one account's session."""
    target = resolve_config_dir(config_dir)
    return f'{CONFIG_DIR_ENV_VAR}="{target}" {executable} /logout'


def resolve_executable(executable: str = "claude") -> str | None:
    """Locate the official CLI, accepting a bare name or an explicit path."""
    found = shutil.which(executable)
    if found:
        return found
    candidate = Path(executable).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None
