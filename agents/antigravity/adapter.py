"""Unified Google Antigravity Provider and Account Adapter.

Antigravity execution is a single parameterized abstraction. Account 1 and
Account 2 share the identical implementation and differ *solely* by declarative
configuration loaded from `config/providers.json`:

    AntigravityAdapter                 (provider-level factory)
      ├── antigravity-account-1        profile ~/.gemini/antigravity-cli
      └── antigravity-account-2        profile ~/.gemini/antigravity-ide

`--app_data_dir` is written in exactly one place in this repository: the
`_base_argv()` method below. Nothing else may construct an Antigravity command
line, which is what keeps the two accounts from ever bleeding into each other.

Verified live against the installed CLI:
  agy --app_data_dir=antigravity-ide --output-format json -p "<prompt>"
  -> exit 0, JSON with conversation_id / status / response / duration_seconds
     / num_turns / usage. The payload carries **no model field**, so the model
     that actually served a request is not verifiable and is reported as
     "unknown" rather than guessed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from agents.base.adapter import (
    UNKNOWN_MODEL,
    AgentAdapter,
    AgentStatus,
    Capability,
    ExecutionMode,
    TaskExecutionResult,
)

#: Single source of truth for the provider configuration file.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "providers.json"

#: How long a deep (network) health probe result stays valid, in seconds.
HEALTH_CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class AntigravityAccountConfig:
    """Declarative configuration for one isolated Antigravity account."""

    agent_id: str
    account_id: str
    #: Value passed verbatim to --app_data_dir; also the profile dir name.
    data_dir: str
    command: str
    fallback_commands: tuple[str, ...] = ()
    output_format: str = "json"
    #: Least privilege by default. Live verification showed headless execution
    #: succeeds without auto-approving tool permissions, so this stays False
    #: and must be turned on deliberately, per account, in providers.json.
    dangerously_skip_permissions: bool = False
    #: Root that contains the per-account profile directories.
    profile_root: Path = Path.home() / ".gemini"
    capabilities: frozenset[Capability] = frozenset()
    models: tuple[str, ...] = ()
    default_model: str = "gemini-3.8-flash-medium"
    default_timeout_seconds: int = 300


class AntigravityAccountAdapter(AgentAdapter):
    """Execution adapter for one isolated Antigravity account profile."""

    def __init__(self, config: AntigravityAccountConfig) -> None:
        self.config = config
        self._bin = self._resolve_binary(config)
        self._current_status = AgentStatus.IDLE
        self._current_task_id: str | None = None
        self._last_conversation_id: str | None = None
        self._health_cache: tuple[float, bool, str] | None = None

    # ------------------------------------------------------------------
    # Binary and profile resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_binary(config: AntigravityAccountConfig) -> Path | None:
        candidates = [config.command, *config.fallback_commands]
        for raw in candidates:
            p = Path(raw).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return p
        for name in ("agy", "antigravity"):
            found = shutil.which(name)
            if found:
                return Path(found)
        return None

    @property
    def profile_dir(self) -> Path:
        return self.config.profile_root / self.config.data_dir

    @property
    def data_dir(self) -> str:
        """The `--app_data_dir` value that isolates this account."""
        return self.config.data_dir

    @property
    def binary(self) -> Path | None:
        return self._bin

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    @property
    def agent_id(self) -> str:
        return self.config.agent_id

    @property
    def provider(self) -> str:
        return "antigravity"

    @property
    def account_id(self) -> str:
        return self.config.account_id

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.HEADLESS

    def capabilities(self) -> frozenset[Capability]:
        return self.config.capabilities

    def available_models(self) -> tuple[str, ...]:
        return self.config.models

    def status(self, task_id: str | None = None) -> AgentStatus:
        if task_id and task_id != self._current_task_id:
            return AgentStatus.IDLE
        return self._current_status

    @property
    def current_task(self) -> str | None:
        return self._current_task_id

    @property
    def current_conversation(self) -> str | None:
        return self._last_conversation_id

    # ------------------------------------------------------------------
    # Command construction — the ONLY place --app_data_dir is emitted
    # ------------------------------------------------------------------
    def _base_argv(self, skip_permissions: bool) -> list[str]:
        argv = [
            str(self._bin),
            f"--app_data_dir={self.config.data_dir}",
            "--output-format",
            self.config.output_format,
        ]
        if skip_permissions:
            # Deliberate, configuration-driven privilege escalation only.
            argv.append("--dangerously-skip-permissions")
        return argv

    def _resolve_skip_permissions(self, options: dict[str, Any] | None) -> bool:
        opts = options or {}
        if "dangerously_skip_permissions" in opts:
            return bool(opts["dangerously_skip_permissions"])
        return self.config.dangerously_skip_permissions

    @staticmethod
    def _redact_argv(argv: list[str]) -> list[str]:
        """Drop prompt content so argv can safely be logged or persisted."""
        safe: list[str] = []
        skip_next = False
        for token in argv:
            if skip_next:
                safe.append("<prompt redacted>")
                skip_next = False
                continue
            safe.append(token)
            if token in ("-p", "--print", "--prompt"):
                skip_next = True
        return safe

    def _subprocess_env(self) -> dict[str, str]:
        """Subprocess environment with GNOME Keyring shielding.

        Point DBUS_SESSION_BUS_ADDRESS to /dev/null so the CLI is forced to use
        its isolated per-profile antigravity-oauth-token and never accesses
        or mutates the system GNOME Keyring.
        """
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = "/dev/null"
        return env

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def health(self, deep: bool = False) -> tuple[bool, str]:
        """Verify this account can execute.

        Lightweight checks (always run, no network, no model usage):
          binary exists and is executable, profile directory exists, is
          readable and writable.

        Deep check (`deep=True`, cached for HEALTH_CACHE_TTL_SECONDS): runs the
        CLI `models` subcommand against this account's profile. That proves the
        CLI responds and the account's session is usable, without spending a
        model request.
        """
        if not self._bin:
            return False, f"Antigravity CLI binary '{self.config.command}' not found"

        profile = self.profile_dir
        if not profile.is_dir():
            return False, f"Account profile directory does not exist: {profile}"
        if not os.access(profile, os.R_OK):
            return False, f"Account profile directory is not readable: {profile}"
        if not os.access(profile, os.W_OK):
            return False, f"Account profile directory is not writable: {profile}"

        shallow_reason = (
            f"READY: {self._bin.name} with isolated profile {self.config.data_dir}"
        )
        if not deep:
            return True, shallow_reason

        now = time.time()
        if self._health_cache and (now - self._health_cache[0]) < HEALTH_CACHE_TTL_SECONDS:
            return self._health_cache[1], self._health_cache[2]

        ok, reason = self._probe_session()
        self._health_cache = (now, ok, reason)
        return ok, reason

    def _probe_session(self) -> tuple[bool, str]:
        """Cheap authenticated round-trip: list models for this profile."""
        try:
            proc = subprocess.run(
                [str(self._bin), f"--app_data_dir={self.config.data_dir}", "models"],
                capture_output=True,
                text=True,
                timeout=90,
                env=self._subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            return False, "Session probe timed out after 90s"
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"Session probe failed: {exc}"

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            tail = detail[-1] if detail else "no diagnostic output"
            return False, f"Session unusable (exit {proc.returncode}): {tail}"

        discovered = self.discover_models(proc.stdout)
        if not discovered:
            return False, "Session responded but advertised no models"
        return True, (
            f"ONLINE: session usable, {len(discovered)} models available "
            f"on profile {self.config.data_dir}"
        )

    @staticmethod
    def discover_models(models_stdout: str) -> tuple[str, ...]:
        """Parse `agy models` output (`id<TAB>Display Name` per line)."""
        found: list[str] = []
        for line in models_stdout.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("fetching"):
                continue
            model_id = line.split("\t")[0].strip()
            if model_id and " " not in model_id:
                found.append(model_id)
        return tuple(found)

    def refresh_models(self) -> tuple[str, ...]:
        """Query the CLI for the live model list for this account."""
        if not self._bin:
            return ()
        try:
            proc = subprocess.run(
                [str(self._bin), f"--app_data_dir={self.config.data_dir}", "models"],
                capture_output=True,
                text=True,
                timeout=90,
                env=self._subprocess_env(),
            )
        except Exception:
            return ()
        if proc.returncode != 0:
            return ()
        return self.discover_models(proc.stdout)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def _failure(
        self,
        task_id: str,
        error: str,
        exit_code: int = 1,
        requested_model: str | None = None,
        duration: float = 0.0,
        command: list[str] | None = None,
        stderr: str = "",
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> TaskExecutionResult:
        return TaskExecutionResult(
            task_id=task_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            provider=self.provider,
            success=False,
            exit_code=exit_code,
            output="",
            error=error,
            requested_model=requested_model,
            actual_model=UNKNOWN_MODEL,
            duration_seconds=duration,
            command=command or [],
            raw_stderr=stderr,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            usage_source="unknown",
            started_at=started_at,
            completed_at=completed_at,
        )

    def _parse_payload(self, stdout: str) -> tuple[dict[str, Any], bool]:
        """Structurally parse the CLI payload. No string scraping of prose."""
        text = stdout.strip()
        if not text:
            return {}, False
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # stream-json emits one JSON object per line; take the last complete
            # object so a streamed run still normalizes structurally.
            for line in reversed(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    return candidate, True
            return {}, False
        if isinstance(parsed, dict):
            return parsed, True
        return {}, False

    def _normalize(
        self,
        task_id: str,
        proc_returncode: int,
        stdout: str,
        stderr: str,
        requested_model: str | None,
        duration: float,
        argv: list[str],
        session_id: str | None,
        fallback_conversation_id: str | None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> TaskExecutionResult:
        payload, json_valid = self._parse_payload(stdout)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

        response_text = payload.get("response") if json_valid else None
        if not isinstance(response_text, str):
            response_text = stdout.strip()

        provider_status = payload.get("status") if isinstance(payload.get("status"), str) else None
        conversation_id = payload.get("conversation_id") or fallback_conversation_id
        if isinstance(conversation_id, str):
            self._last_conversation_id = conversation_id

        # A provider status of anything other than SUCCESS is a failure even if
        # the process exited 0, so a soft error is never reported as success.
        success = proc_returncode == 0 and (
            provider_status is None or provider_status.upper() == "SUCCESS"
        )

        # An empty response is not a success either. Headless mode auto-denies
        # any tool that needs a permission prompt, which yields exit 0, status
        # SUCCESS and no answer at all. Reporting that as done would be a lie.
        empty_response = not response_text.strip()
        if success and empty_response:
            success = False

        error_text = stderr.strip()
        if not success and not error_text:
            error_text = (
                payload.get("error")
                or (
                    "Provider produced no output"
                    if empty_response
                    else f"Provider reported status={provider_status!r} (exit {proc_returncode})"
                )
            )
        if not success and empty_response and "permission" in error_text.lower():
            error_text += (
                "\nHint: this task needs tool permissions. Re-run it with "
                "--allow-tool-permissions, or add a scoped rule under "
                "permissions.allow in the account profile's settings.json "
                "(preferred, least privilege)."
            )

        provider_duration = payload.get("duration_seconds")
        has_usage = bool(usage) and any(usage.get(k) is not None for k in ("input_tokens", "output_tokens", "total_tokens"))
        in_tok = int(usage["input_tokens"]) if usage.get("input_tokens") is not None else None
        out_tok = int(usage["output_tokens"]) if usage.get("output_tokens") is not None else None
        tot_tok = int(usage["total_tokens"]) if usage.get("total_tokens") is not None else None
        usage_source = "reported" if has_usage else "unknown"

        return TaskExecutionResult(
            task_id=task_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            provider=self.provider,
            success=success,
            exit_code=proc_returncode,
            output=response_text,
            error=error_text if not success else stderr.strip(),
            requested_model=requested_model,
            # The Antigravity JSON payload carries no model field. Only report a
            # model when the provider actually names one.
            actual_model=self._extract_actual_model(payload),
            duration_seconds=duration,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            usage_source=usage_source,
            thinking_tokens=int(usage.get("thinking_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
            num_turns=int(payload.get("num_turns") or 0),
            conversation_id=conversation_id,
            session_id=session_id,
            provider_status=provider_status,
            provider_duration_seconds=float(provider_duration) if isinstance(provider_duration, (int, float)) else None,
            json_valid=json_valid,
            raw_response=payload,
            raw_stdout=stdout,
            raw_stderr=stderr,
            command=self._redact_argv(argv),
            started_at=started_at,
            completed_at=completed_at,
        )

    @staticmethod
    def _extract_actual_model(payload: dict[str, Any]) -> str:
        for key in ("actual_model", "model", "model_used", "model_version"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return UNKNOWN_MODEL

    def execute(
        self,
        task_id: str,
        prompt: str,
        model: str | None = None,
        work_dir: Path | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        opts = dict(options or {})
        timeout = timeout_seconds or self.config.default_timeout_seconds
        requested_model = model or self.config.default_model
        session_id = opts.get("session_id")
        conversation_id = opts.get("conversation_id")

        if not self._bin:
            return self._failure(
                task_id,
                f"Antigravity CLI binary '{self.config.command}' not resolved",
                requested_model=requested_model,
            )

        argv = self._base_argv(self._resolve_skip_permissions(opts))
        if requested_model and requested_model != "auto":
            argv.append(f"--model={requested_model}")
        if conversation_id:
            argv.append(f"--conversation={conversation_id}")
        if opts.get("mode") in ("plan", "accept-edits"):
            argv.append(f"--mode={opts['mode']}")
        if opts.get("effort") in ("low", "medium", "high"):
            argv.append(f"--effort={opts['effort']}")
        if opts.get("disable_slash_commands"):
            argv.append("--disable-slash-commands")
        argv.extend(["-p", prompt])

        self._current_status = AgentStatus.WORKING
        self._current_task_id = task_id
        started = time.time()
        import datetime
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(work_dir or Path.cwd()),
                env=self._subprocess_env(),
            )
        except subprocess.TimeoutExpired:
            self._current_status = AgentStatus.FAILED
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return self._failure(
                task_id,
                f"Execution timed out after {timeout} seconds",
                exit_code=124,
                requested_model=requested_model,
                duration=time.time() - started,
                command=self._redact_argv(argv),
                started_at=started_at,
                completed_at=completed_at,
            )
        except Exception as exc:
            self._current_status = AgentStatus.FAILED
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return self._failure(
                task_id,
                str(exc),
                requested_model=requested_model,
                duration=time.time() - started,
                command=self._redact_argv(argv),
                started_at=started_at,
                completed_at=completed_at,
            )

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result = self._normalize(
            task_id=task_id,
            proc_returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            requested_model=requested_model,
            duration=time.time() - started,
            argv=argv,
            session_id=session_id,
            fallback_conversation_id=conversation_id,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._current_status = AgentStatus.IDLE if result.success else AgentStatus.FAILED
        self._current_task_id = None
        return result

    def continue_session(
        self,
        task_id: str,
        session_id: str,
        prompt: str,
        model: str | None = None,
        work_dir: Path | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> TaskExecutionResult:
        """Resume an Antigravity conversation.

        `session_id` is the Antigravity `conversation_id`. Callers that hold a
        brain session id should pass the mapped conversation id from the
        SessionManager, or set `options["conversation_id"]` explicitly.
        """
        opts = dict(options or {})
        opts.setdefault("conversation_id", session_id)
        return self.execute(
            task_id=task_id,
            prompt=prompt,
            model=model,
            work_dir=work_dir,
            timeout_seconds=timeout_seconds,
            options=opts,
        )

    def stream(
        self,
        task_id: str,
        prompt: str,
        model: str | None = None,
        work_dir: Path | None = None,
        timeout_seconds: int | None = None,
        options: dict[str, Any] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> TaskExecutionResult:
        """Execute with incremental NDJSON events via --output-format stream-json."""
        opts = dict(options or {})
        timeout = timeout_seconds or self.config.default_timeout_seconds
        requested_model = model or self.config.default_model
        session_id = opts.get("session_id")
        conversation_id = opts.get("conversation_id")

        if not self._bin:
            return self._failure(
                task_id,
                f"Antigravity CLI binary '{self.config.command}' not resolved",
                requested_model=requested_model,
            )

        argv = [
            str(self._bin),
            f"--app_data_dir={self.config.data_dir}",
            "--output-format",
            "stream-json",
        ]
        if self._resolve_skip_permissions(opts):
            argv.append("--dangerously-skip-permissions")
        if requested_model and requested_model != "auto":
            argv.append(f"--model={requested_model}")
        if conversation_id:
            argv.append(f"--conversation={conversation_id}")
        argv.extend(["-p", prompt])

        self._current_status = AgentStatus.WORKING
        self._current_task_id = task_id
        started = time.time()
        collected: list[str] = []
        stderr_text = ""
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(work_dir or Path.cwd()),
                env=self._subprocess_env(),
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                collected.append(line)
                if on_event:
                    stripped = line.strip()
                    if stripped:
                        try:
                            on_event(json.loads(stripped))
                        except json.JSONDecodeError:
                            on_event({"type": "raw", "line": stripped})
            proc.wait(timeout=timeout)
            stderr_text = proc.stderr.read() if proc.stderr else ""
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            self._current_status = AgentStatus.FAILED
            return self._failure(
                task_id,
                f"Streaming execution timed out after {timeout} seconds",
                exit_code=124,
                requested_model=requested_model,
                duration=time.time() - started,
                command=self._redact_argv(argv),
            )
        except Exception as exc:
            self._current_status = AgentStatus.FAILED
            return self._failure(
                task_id,
                str(exc),
                requested_model=requested_model,
                duration=time.time() - started,
                command=self._redact_argv(argv),
            )

        result = self._normalize(
            task_id=task_id,
            proc_returncode=returncode,
            stdout="".join(collected),
            stderr=stderr_text,
            requested_model=requested_model,
            duration=time.time() - started,
            argv=argv,
            session_id=session_id,
            fallback_conversation_id=conversation_id,
        )
        self._current_status = AgentStatus.IDLE if result.success else AgentStatus.FAILED
        self._current_task_id = None
        return result

    def cancel(self, task_id: str) -> bool:
        """Antigravity print-mode runs are single-shot subprocesses.

        There is no server-side cancel API, so cancellation only clears local
        in-flight state; it never claims to have stopped remote work.
        """
        if self._current_task_id and self._current_task_id != task_id:
            return False
        self._current_status = AgentStatus.IDLE
        self._current_task_id = None
        return True

    def describe(self) -> dict[str, Any]:
        base = super().describe()
        base.update(
            {
                "data_dir": self.config.data_dir,
                "output_format": self.config.output_format,
                "dangerously_skip_permissions": self.config.dangerously_skip_permissions,
                "default_model": self.config.default_model,
                "current_task": self._current_task_id,
                "current_conversation": self._last_conversation_id,
            }
        )
        return base


class AntigravityAdapter:
    """Provider-level factory holding every configured Antigravity account."""

    @staticmethod
    def _config_from_dict(
        agent_key: str,
        account_conf: dict[str, Any],
        command: str,
        fallback_commands: tuple[str, ...],
        profile_root: Path,
    ) -> AntigravityAccountConfig:
        capabilities = frozenset(
            Capability(c) for c in account_conf.get("capabilities", []) if c in Capability._value2member_map_
        )
        execution = account_conf.get("execution", {})
        return AntigravityAccountConfig(
            agent_id=account_conf.get("agent_id", agent_key),
            account_id=account_conf.get("account_id", agent_key),
            data_dir=execution.get("app_data_dir") or account_conf.get("data_dir", "antigravity"),
            command=execution.get("command") or command,
            fallback_commands=fallback_commands,
            output_format=execution.get("output_format", "json"),
            dangerously_skip_permissions=bool(
                execution.get("dangerously_skip_permissions", False)
            ),
            profile_root=profile_root,
            capabilities=capabilities,
            models=tuple(account_conf.get("models", ())),
            default_model=account_conf.get("default_model", "gemini-3.8-flash-medium"),
            default_timeout_seconds=int(execution.get("default_timeout_seconds", 300)),
        )

    @classmethod
    def load_from_config(
        cls, config_path: Path | str | None = None
    ) -> list[AntigravityAccountAdapter]:
        if config_path:
            path = Path(config_path)
        else:
            # Honour BRAIN_PROVIDERS_CONFIG like every other config reader, so a
            # test or sandbox never silently falls back to the live file.
            from providers.registry.config import resolve_config_path

            path = resolve_config_path()
        if not path.is_file():
            raise FileNotFoundError(
                f"Provider configuration not found: {path}. Antigravity accounts are "
                "configuration-driven and are never hard-coded."
            )

        data = json.loads(path.read_text(encoding="utf-8"))
        provider_conf = data.get("providers", {}).get("antigravity", {})
        command = provider_conf.get("command", "agy")
        fallback_commands = tuple(provider_conf.get("fallback_commands", ()))
        profile_root = Path(
            provider_conf.get("profile_root", str(Path.home() / ".gemini"))
        ).expanduser()

        adapters: list[AntigravityAccountAdapter] = []
        for agent_key, account_conf in provider_conf.get("accounts", {}).items():
            if not account_conf.get("enabled", True):
                continue
            adapters.append(
                AntigravityAccountAdapter(
                    cls._config_from_dict(
                        agent_key, account_conf, command, fallback_commands, profile_root
                    )
                )
            )
        return adapters

    @classmethod
    def get_account(
        cls, agent_id: str, config_path: Path | str | None = None
    ) -> AntigravityAccountAdapter:
        for adapter in cls.load_from_config(config_path):
            if adapter.agent_id == agent_id:
                return adapter
        from providers.registry.config import resolve_config_path

        raise RuntimeError(f"{agent_id} is not defined in {config_path or resolve_config_path()}")
