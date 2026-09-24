"""Claude Code headless execution adapter.

One adapter instance is one Claude account. Accounts are isolated from each
other by ``CLAUDE_CONFIG_DIR``, the same mechanism Antigravity accounts use with
``--app_data_dir``: two accounts never share a session, a token, or a rate-limit
budget, and adding one cannot disturb another.

Flags verified against `claude --help` on 2.1.280:
  ``-p/--print`` for headless, ``--output-format json`` for a structured result,
  ``--model <id>``, ``--resume <session-id>`` to continue, ``--fallback-model``,
  ``--dangerously-skip-permissions`` (off unless explicitly requested).

Two behaviours of the real CLI drive the parsing here, both verified live:

  1. **Exit code 0 is not success.** A rate-limited run exits 0 and reports the
     failure only in the JSON body as ``is_error: true`` with
     ``api_error_status: 429``. Trusting the exit code would record a quota wall
     as a completed task and hand downstream agents an error string as content.
  2. **`subtype` is not success either.** The same rate-limited run reported
     ``subtype: "success"`` alongside ``is_error: true``. ``is_error`` is the
     only field that told the truth, so it is the one that decides.

Token savings: when an account is configured with ``use_pxpipe``, the subprocess
gets ``ANTHROPIC_BASE_URL`` pointed at the local pxpipe proxy, which rewrites the
bulky parts of each request as images before it leaves the machine. The proxy is
loopback-only and never sees a credential it did not receive from this CLI.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from agents.base.adapter import (
    UNKNOWN_MODEL,
    AgentAdapter,
    AgentStatus,
    Capability,
    ExecutionMode,
    TaskExecutionResult,
)
from agents.claude.auth import (
    CONFIG_DIR_ENV_VAR,
    ClaudeAuthMode,
    ProfileStatus,
    inspect_profile,
    login_command,
    resolve_config_dir,
    resolve_executable,
)

_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

#: Upstream statuses that mean "this account is temporarily out of capacity, try
#: another one" rather than "this task is broken". The brain's failover engine
#: keys on this to move a task to a sibling account instead of failing it.
_RATE_LIMIT_STATUSES = frozenset({429, 529})

#: Substrings that identify a quota wall when no numeric status is reported.
#: Matched case-insensitively against the result text.
_RATE_LIMIT_MARKERS = (
    "weekly limit",
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota",
    "resource_exhausted",
    "overloaded",
)


def classify_failure(api_error_status: int | None, message: str) -> str:
    """Name the failure class so the failover engine can act on it.

    Returns ``rate_limit`` for a recoverable capacity wall (retry elsewhere),
    ``auth`` for a credential problem (retrying elsewhere is pointless until the
    human logs in), or ``error`` for everything else.
    """
    if api_error_status in _RATE_LIMIT_STATUSES:
        return "rate_limit"
    if api_error_status in (401, 403):
        return "auth"
    lowered = (message or "").lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if "unauthorized" in lowered or "invalid api key" in lowered or "please run /login" in lowered:
        return "auth"
    return "error"


class ClaudeAdapter(AgentAdapter):
    """Adapter for one headless Claude Code account.

    Capabilities, models, profile directory and authentication mode all come from
    ``config/providers.json``; the literals below are a last-resort fallback so
    the adapter still functions if the config file is unavailable.
    """

    DEFAULT_CAPABILITIES = frozenset({
        Capability.DEEP_REASONING,
        Capability.ARCHITECTURE,
        Capability.CODE_REVIEW,
        Capability.COMPONENT_REFACTORING,
        Capability.EDITOR_REFACTORING,
        Capability.TEST_SCAFFOLDING,
        Capability.DOCUMENTATION,
    })
    DEFAULT_MODELS = ("sonnet", "opus", "haiku")

    #: Multi-account by design, and verified: CLAUDE_CONFIG_DIR relocates the
    #: entire profile, so each account has its own credential and session store.
    multi_account: bool = True

    def __init__(
        self,
        executable: str = "claude",
        capabilities: frozenset[Capability] | None = None,
        models: tuple[str, ...] | None = None,
        default_model: str = "sonnet",
        agent_id: str = "claude-account-1",
        account_id: str = "account-1",
        config_dir: str | Path | None = None,
        auth_mode: ClaudeAuthMode | str = ClaudeAuthMode.SUBSCRIPTION,
        credential_reference: str = "",
        credential_resolver: Any | None = None,
        pxpipe_base_url: str | None = None,
        fallback_model: str | None = None,
        default_timeout_seconds: int = 300,
    ) -> None:
        self._executable = executable
        self._capabilities = capabilities or self.DEFAULT_CAPABILITIES
        self._models = models or self.DEFAULT_MODELS
        self._default_model = default_model
        self._agent_id = agent_id
        self._account_id = account_id
        self._config_dir = resolve_config_dir(config_dir)
        self._auth_mode = (
            auth_mode if isinstance(auth_mode, ClaudeAuthMode) else ClaudeAuthMode.parse(auth_mode)
        )
        self._credential_reference = credential_reference
        self._credential_resolver = credential_resolver
        self._pxpipe_base_url = (pxpipe_base_url or "").strip() or None
        self._fallback_model = fallback_model
        self._default_timeout_seconds = default_timeout_seconds
        self._current_status = AgentStatus.IDLE

    # --- Identity ---------------------------------------------------------

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def provider(self) -> str:
        return "claude"

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.HEADLESS

    @property
    def profile_dir(self) -> Path | None:
        return self._config_dir

    @property
    def auth_mode(self) -> ClaudeAuthMode:
        return self._auth_mode

    @property
    def pxpipe_base_url(self) -> str | None:
        """The proxy this account's traffic is routed through, if any."""
        return self._pxpipe_base_url

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def available_models(self) -> tuple[str, ...]:
        return self._models

    def status(self, task_id: str | None = None) -> AgentStatus:
        return self._current_status

    # --- Health -----------------------------------------------------------

    def profile_status(self) -> ProfileStatus:
        """Login metadata for this account. Never contains a credential."""
        return inspect_profile(self._config_dir)

    def health(self, deep: bool = False) -> tuple[bool, str]:
        """Report whether this account could run a task right now.

        For a subscription account this checks that the human actually logged in
        to *this* profile, because that is the failure an operator hits when
        adding a second account and the one that is otherwise reported as an
        opaque runtime error.
        """
        path = resolve_executable(self._executable)
        if not path:
            return False, f"Executable {self._executable} not found on PATH"

        if self._auth_mode is ClaudeAuthMode.SUBSCRIPTION:
            profile = self.profile_status()
            if not profile.logged_in:
                return False, f"{profile.reason}; run: {login_command(self._config_dir, self._executable)}"
            detail = f"subscription {profile.plan or 'active'} in {profile.config_dir}"
            if profile.world_readable:
                return True, f"{detail} (WARNING: credential file is readable by other local users)"
            if profile.expiring_soon:
                return True, f"{detail} (credential expires in {profile.seconds_until_expiry}s)"
            return True, detail

        secret = self._resolve_secret()
        if not secret:
            label = "CLAUDE_CODE_OAUTH_TOKEN" if self._auth_mode is ClaudeAuthMode.OAUTH_TOKEN else "ANTHROPIC_API_KEY"
            return False, (
                f"{self._auth_mode.value} account has no resolvable credential "
                f"(reference {self._credential_reference or 'unset'}, env {label})"
            )
        return True, f"{self._auth_mode.value} credential resolved for {self._agent_id}"

    # --- Credential resolution -------------------------------------------

    def _resolve_secret(self) -> str | None:
        """Fetch this account's secret for one subprocess, for non-subscription modes.

        Subscription accounts never reach here: their credential belongs to the
        official CLI and is read from the profile dir by that CLI, not by us.
        The returned value is passed straight into the child environment and is
        never logged, stored on the instance, or included in a result object.
        """
        if self._auth_mode is ClaudeAuthMode.SUBSCRIPTION:
            return None
        if self._credential_reference and self._credential_resolver is not None:
            try:
                resolved = self._credential_resolver.resolve(self._credential_reference)
                if resolved:
                    return str(resolved)
            except Exception:
                # A credential backend that is down must not crash routing; the
                # env fallback below still gives the account a chance to run.
                pass
        env_var = (
            "CLAUDE_CODE_OAUTH_TOKEN"
            if self._auth_mode is ClaudeAuthMode.OAUTH_TOKEN
            else "ANTHROPIC_API_KEY"
        )
        return os.environ.get(env_var, "").strip() or None

    def _build_env(self) -> dict[str, str]:
        """Environment for one execution: account isolation, then auth, then proxy."""
        env = os.environ.copy()
        env[CONFIG_DIR_ENV_VAR] = str(self._config_dir)

        # Never let an ambient credential silently outrank the account's own
        # identity. A shell that exports ANTHROPIC_API_KEY would otherwise bill
        # every "subscription" account to that key without anyone noticing.
        for leaked in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(leaked, None)

        if self._auth_mode is not ClaudeAuthMode.SUBSCRIPTION:
            secret = self._resolve_secret()
            if secret:
                if self._auth_mode is ClaudeAuthMode.OAUTH_TOKEN:
                    env["CLAUDE_CODE_OAUTH_TOKEN"] = secret
                else:
                    env["ANTHROPIC_API_KEY"] = secret

        if self._pxpipe_base_url:
            env["ANTHROPIC_BASE_URL"] = self._pxpipe_base_url
        return env

    # --- Execution --------------------------------------------------------

    def execute(
        self,
        task_id: str,
        prompt: str,
        model: str | None = None,
        work_dir: Path | None = None,
        timeout_seconds: int = 300,
        options: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        opts = options or {}
        exe = resolve_executable(self._executable)
        if not exe:
            return self._failure(task_id, f"{self._executable} not found on PATH", model)

        healthy, reason = self.health()
        if not healthy:
            # Failing here rather than at the CLI keeps the actionable message
            # ("run this login command") instead of an opaque subprocess error.
            return self._failure(task_id, reason, model, failure_class="auth")

        target_model = model or self._default_model
        cmd = [exe, "-p", "--output-format", "json"]
        if target_model:
            cmd.extend(["--model", target_model])
        fallback = opts.get("fallback_model") or self._fallback_model
        if fallback:
            cmd.extend(["--fallback-model", str(fallback)])
        if opts.get("resume_id") or opts.get("session_id"):
            cmd.extend(["--resume", str(opts.get("resume_id") or opts.get("session_id"))])
        if opts.get("dangerously_skip_permissions") or opts.get("trust_all_tools"):
            cmd.append("--dangerously-skip-permissions")
        for extra_dir in opts.get("add_dirs", ()) or ():
            cmd.extend(["--add-dir", str(extra_dir)])
        # The prompt is passed as a positional argument, never interpolated into
        # a shell string: subprocess is invoked without a shell so a prompt
        # containing quotes, backticks or newlines cannot become a command.
        cmd.append(prompt)

        self._current_status = AgentStatus.WORKING
        start = time.time()
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds or self._default_timeout_seconds,
                cwd=str(work_dir or Path.cwd()),
                env=self._build_env(),
            )
        except subprocess.TimeoutExpired:
            self._current_status = AgentStatus.FAILED
            return self._failure(
                task_id,
                f"Task timed out after {timeout_seconds}s",
                target_model,
                exit_code=124,
                duration=time.time() - start,
                started_at=started_at,
                completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                failure_class="timeout",
            )
        except Exception as exc:
            self._current_status = AgentStatus.FAILED
            return self._failure(
                task_id,
                f"{exc.__class__.__name__}: {exc}",
                target_model,
                duration=time.time() - start,
                started_at=started_at,
                completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self._parse(
            task_id=task_id,
            proc=proc,
            cmd=cmd,
            requested_model=target_model,
            duration=time.time() - start,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _parse(
        self,
        task_id: str,
        proc: subprocess.CompletedProcess[str],
        cmd: list[str],
        requested_model: str | None,
        duration: float,
        started_at: str,
        completed_at: str,
    ) -> TaskExecutionResult:
        """Normalize one CLI invocation into the cross-provider result contract."""
        stdout = _ANSI_ESCAPE.sub("", proc.stdout or "").strip()
        stderr = _ANSI_ESCAPE.sub("", proc.stderr or "").strip()

        payload: dict[str, Any] = {}
        json_valid = False
        if stdout:
            try:
                candidate = json.loads(stdout)
                if isinstance(candidate, dict):
                    payload = candidate
                    json_valid = True
            except json.JSONDecodeError:
                json_valid = False

        # `is_error` is authoritative. Verified live: a rate-limited run exits 0
        # and reports subtype "success" while is_error is true.
        is_error = bool(payload.get("is_error")) if json_valid else True
        api_error_status = payload.get("api_error_status")
        api_error_status = int(api_error_status) if isinstance(api_error_status, (int, float)) else None
        text = str(payload.get("result") or "") if json_valid else stdout
        success = json_valid and not is_error and bool(text.strip())

        if success:
            error = ""
            failure_class = ""
        elif not json_valid:
            error = stderr or stdout or f"Claude produced no parseable JSON (exit {proc.returncode})"
            failure_class = classify_failure(None, error)
        else:
            error = text.strip() or stderr or f"Claude reported an error (exit {proc.returncode})"
            failure_class = classify_failure(api_error_status, error)

        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        thinking = 0
        details = usage.get("output_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("thinking_tokens"), (int, float)):
            thinking = int(details["thinking_tokens"])

        def _int(key: str) -> int | None:
            value = usage.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        input_tokens = _int("input_tokens")
        output_tokens = _int("output_tokens")
        cache_read = _int("cache_read_input_tokens") or 0
        cache_create = _int("cache_creation_input_tokens") or 0
        total = None
        if input_tokens is not None or output_tokens is not None:
            total = (input_tokens or 0) + (output_tokens or 0) + cache_read + cache_create

        provider_duration = payload.get("duration_ms")
        provider_duration_s = (
            float(provider_duration) / 1000.0 if isinstance(provider_duration, (int, float)) else None
        )

        self._current_status = AgentStatus.IDLE if success else AgentStatus.FAILED

        raw_response: dict[str, Any] = dict(payload)
        if failure_class:
            # Surfaced for the failover engine, which routes rate_limit to a
            # sibling account and stops on auth instead of burning the pool.
            raw_response["failure_class"] = failure_class
        if self._pxpipe_base_url:
            raw_response["pxpipe_base_url"] = self._pxpipe_base_url

        return TaskExecutionResult(
            task_id=task_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            provider=self.provider,
            success=success,
            exit_code=proc.returncode,
            output=text.strip() if success else "",
            error=error,
            requested_model=requested_model,
            # The CLI does not echo the resolved model id, so the sentinel is
            # used rather than asserting the requested id was the one served.
            actual_model=UNKNOWN_MODEL,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total,
            usage_source="provider" if usage else "unknown",
            started_at=started_at,
            completed_at=completed_at,
            conversation_id=str(payload.get("session_id")) if payload.get("session_id") else None,
            session_id=str(payload.get("session_id")) if payload.get("session_id") else None,
            provider_status=str(payload.get("subtype")) if payload.get("subtype") else None,
            provider_duration_seconds=provider_duration_s,
            num_turns=int(payload["num_turns"]) if isinstance(payload.get("num_turns"), (int, float)) else 0,
            thinking_tokens=thinking,
            cache_read_tokens=cache_read,
            json_valid=json_valid,
            raw_response=raw_response,
            raw_stdout=proc.stdout or "",
            raw_stderr=proc.stderr or "",
            command=cmd[:-1] + ["<prompt redacted>"],
        )

    def _failure(
        self,
        task_id: str,
        error: str,
        model: str | None,
        exit_code: int = 1,
        duration: float = 0.0,
        started_at: str | None = None,
        completed_at: str | None = None,
        failure_class: str = "error",
    ) -> TaskExecutionResult:
        self._current_status = AgentStatus.FAILED
        return TaskExecutionResult(
            task_id=task_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            provider=self.provider,
            success=False,
            exit_code=exit_code,
            output="",
            error=error,
            requested_model=model,
            actual_model=UNKNOWN_MODEL,
            duration_seconds=duration,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            usage_source="unknown",
            started_at=started_at,
            completed_at=completed_at,
            raw_response={"failure_class": failure_class},
        )

    def continue_session(
        self,
        task_id: str,
        session_id: str,
        prompt: str,
        model: str | None = None,
        work_dir: Path | None = None,
        timeout_seconds: int = 300,
        options: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        """Resume a Claude conversation by session id."""
        opts = dict(options or {})
        opts.setdefault("resume_id", session_id)
        return self.execute(task_id, prompt, model, work_dir, timeout_seconds, opts)

    def cancel(self, task_id: str) -> bool:
        self._current_status = AgentStatus.IDLE
        return True

    def describe(self) -> dict[str, Any]:
        """Registry-facing description, extended with auth and proxy state."""
        base = super().describe()
        base["auth_mode"] = self._auth_mode.value
        base["pxpipe_base_url"] = self._pxpipe_base_url
        if self._auth_mode is ClaudeAuthMode.SUBSCRIPTION:
            base["subscription"] = self.profile_status().to_dict()
        return base

    # --- Config-driven construction --------------------------------------

    @classmethod
    def load_from_config(
        cls,
        config_path: str | Path | None = None,
        credential_resolver: Any | None = None,
        pxpipe_base_url: str | None = None,
    ) -> list[ClaudeAdapter]:
        """Build one adapter per enabled Claude account in providers.json.

        Import is local so that this module stays usable on its own (tests, the
        auth CLI) without pulling the whole provider registry in.
        """
        from providers.registry.config import get_accounts, get_provider_meta

        path = str(config_path) if config_path else None
        meta = get_provider_meta("claude", path)
        if not meta or not meta.get("enabled", True):
            return []

        adapters: list[ClaudeAdapter] = []
        for account in get_accounts("claude", path):
            if not account.enabled:
                continue
            raw = account.raw
            execution = raw.get("execution", {}) if isinstance(raw.get("execution"), dict) else {}
            use_pxpipe = bool(raw.get("use_pxpipe", meta.get("use_pxpipe", False)))
            adapters.append(
                cls(
                    executable=account.command,
                    capabilities=account.capabilities or None,
                    models=account.models or None,
                    default_model=account.default_model or "sonnet",
                    agent_id=account.agent_id,
                    account_id=account.account_id,
                    config_dir=raw.get("config_dir"),
                    auth_mode=ClaudeAuthMode.parse(
                        raw.get("auth_mode") or raw.get("authentication_type")
                    ),
                    credential_reference=raw.get("credential_reference", ""),
                    credential_resolver=credential_resolver,
                    pxpipe_base_url=pxpipe_base_url if use_pxpipe else None,
                    fallback_model=raw.get("fallback_model") or meta.get("fallback_model"),
                    default_timeout_seconds=int(execution.get("default_timeout_seconds", 300)),
                )
            )
        return adapters
