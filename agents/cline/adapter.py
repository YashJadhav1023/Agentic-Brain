"""Cline CLI Agent Adapter.

Flags verified against `cline --help`:
  the prompt is positional, `-p/--plan` is PLAN MODE (not print),
  `--json` emits machine-readable messages, `-m/--model <id>` selects the model,
  `--id <session-id>` resumes a session, and `--auto-approve <boolean>`
  defaults to **true**.

Because auto-approval is on by default in this CLI, the adapter explicitly sends
`--auto-approve false` unless a caller opts in. Least privilege has to be stated,
not assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from agents.cline import free_routing
from agents.base.adapter import (
    UNKNOWN_MODEL,
    AgentAdapter,
    AgentStatus,
    Capability,
    ExecutionMode,
    TaskExecutionResult,
)


class ClineAdapter(AgentAdapter):
    """Adapter for the headless Cline CLI."""

    DEFAULT_CAPABILITIES = frozenset({
        Capability.EDITOR_REFACTORING,
        Capability.FRONTEND_STYLING,
        Capability.COMPONENT_REFACTORING,
        Capability.CODE_REVIEW,
    })
    #: "auto" is deliberately absent: it hands model choice back to Cline's
    #: persisted state, which is the paid-drift path free_routing guards against.
    DEFAULT_MODELS = free_routing.FREE_MODELS

    def __init__(
        self,
        executable: str = "cline",
        capabilities: frozenset[Capability] | None = None,
        models: tuple[str, ...] | None = None,
        default_model: str = free_routing.DEFAULT_FREE_MODEL,
        agent_id: str = "cline",
        account_id: str = "cli",
        config_dir: str | Path | None = None,
        data_dir: str | Path | None = None,
    ) -> None:
        self._executable = executable
        self._capabilities = capabilities or self.DEFAULT_CAPABILITIES
        self._models = models or self.DEFAULT_MODELS
        self._default_model = default_model
        self._agent_id = agent_id
        self._account_id = account_id
        self._config_dir = Path(config_dir).expanduser() if config_dir else None
        self._data_dir = Path(data_dir).expanduser() if data_dir else None
        self._current_status = AgentStatus.IDLE

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def provider(self) -> str:
        # Agent identity, per the base-class contract ("antigravity", "kiro",
        # "cline"). Dispatch keys off this (see providers/adapters/bridge.py), so it
        # must NOT be changed to the upstream route. Use `routing_provider` for the
        # provider requests are actually sent to.
        return "cline"

    @property
    def routing_provider(self) -> str:
        """The upstream provider requests are actually sent to via `-P`.

        Distinct from `provider`, which is this agent's identity. Cline's own
        provider bills Cline Credits, so execution is pinned to the free Gemini
        route; cost attribution should read this, not `provider`.
        """
        return free_routing.FREE_PROVIDER if free_routing.enforcement_enabled() else "cline"

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def config_dir(self) -> Path | None:
        return self._config_dir

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.HEADLESS

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def available_models(self) -> tuple[str, ...]:
        models = list(self._models)
        if self._data_dir:
            prov_file = self._data_dir / "settings" / "providers.json"
            if prov_file.is_file():
                try:
                    import json
                    d = json.loads(prov_file.read_text(encoding="utf-8"))
                    for pdata in d.get("providers", {}).values():
                        setts = pdata.get("settings", {})
                        m = setts.get("model")
                        if m and m not in models:
                            models.insert(0, m)
                except Exception:
                    pass
        # Models discovered from providers.json are hoisted to the front above, which
        # made whatever a provider happened to hold the *preferred* choice. When a
        # paid model was parked there, that silently became the default. Filtering
        # last keeps discovery useful while refusing to offer a billed model.
        return free_routing.free_models_only(models)

    @property
    def models(self) -> tuple[str, ...]:
        return self.available_models()

    def status(self, task_id: str | None = None) -> AgentStatus:
        return self._current_status

    def _resolve_executable(self) -> str | None:
        found = shutil.which(self._executable)
        if found:
            return found
        candidate = Path(self._executable).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None

    def health(self, deep: bool = False) -> tuple[bool, str]:
        path = self._resolve_executable()
        if path:
            return True, f"Found {self._executable} at {path}"
        return False, f"Executable {self._executable} not found on PATH"

    @staticmethod
    def _extract_response(stdout: str) -> tuple[str, dict[str, Any], bool, int | None, int | None, int | None, str | None]:
        """Parse --json NDJSON output; extract text, usage, and model."""
        text = stdout.strip()
        if not text:
            return "", {}, False, None, None, None, None
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                messages.append(obj)
        if not messages:
            return text, {}, False, None, None, None, None

        # Look for run_result first, as it contains final text, aggregate usage and model info
        run_result: dict[str, Any] | None = None
        for msg in reversed(messages):
            if msg.get("type") == "run_result":
                run_result = msg
                break

        target_payload = run_result or messages[-1]

        # Extract response text
        response_text = ""
        for key in ("text", "content", "message", "response", "result"):
            val = target_payload.get(key)
            if isinstance(val, str) and val.strip():
                response_text = val.strip()
                break

        if not response_text:
            # Fallback: scan backwards for any text payload
            for msg in reversed(messages):
                ev = msg.get("event") if isinstance(msg.get("event"), dict) else msg
                for key in ("text", "content", "message", "response", "result"):
                    val = ev.get(key)
                    if isinstance(val, str) and val.strip():
                        response_text = val.strip()
                        break
                if response_text:
                    break

        if not response_text:
            response_text = text

        # Extract token usage
        input_tokens: int | None = None
        output_tokens: int | None = None
        total_tokens: int | None = None

        usage = None
        if run_result:
            usage = run_result.get("aggregateUsage") or run_result.get("usage")
        if not usage:
            for msg in reversed(messages):
                ev = msg.get("event") if isinstance(msg.get("event"), dict) else msg
                if "usage" in ev and isinstance(ev["usage"], dict):
                    usage = ev["usage"]
                    break

        if isinstance(usage, dict):
            inp = usage.get("totalInputTokens") or usage.get("inputTokens")
            out = usage.get("totalOutputTokens") or usage.get("outputTokens")
            if inp is not None:
                input_tokens = int(inp)
            if out is not None:
                output_tokens = int(out)
            if input_tokens is not None or output_tokens is not None:
                total_tokens = (input_tokens or 0) + (output_tokens or 0)

        # Extract model
        model_id: str | None = None
        model_field = target_payload.get("model")
        if isinstance(model_field, dict):
            model_id = model_field.get("id") or model_field.get("name")
        elif isinstance(model_field, str) and model_field.strip():
            model_id = model_field.strip()

        if not model_id and run_result:
            rf_model = run_result.get("model")
            if isinstance(rf_model, dict):
                model_id = rf_model.get("id") or rf_model.get("name")
            elif isinstance(rf_model, str):
                model_id = rf_model.strip()

        return response_text, target_payload, True, input_tokens, output_tokens, total_tokens, model_id

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
        exe = self._resolve_executable()
        if not exe:
            return self._failure(task_id, f"{self._executable} not found on PATH", model)

        # Headless execution requires auto_approve true so CLI tools do not hang waiting for interactive stdin
        auto_approve = opts.get("auto_approve", True)
        if opts.get("auto_approve") is False:
            auto_approve = False

        cmd = [exe, "--json", "--auto-approve", "true" if auto_approve else "false"]
        if self._config_dir:
            cmd.extend(["--config", str(self._config_dir)])
        if self._data_dir:
            cmd.extend(["--data-dir", str(self._data_dir)])
        # Correct any drift in Cline's persisted provider/model before running. This
        # state is rewritten by any interactive session and was observed back on the
        # paid `cline` provider, so it is re-pinned per run rather than assumed.
        free_routing.pin_free_route(self._config_dir, self._data_dir)
        # The provider is pinned explicitly. Without -P, Cline uses its persisted
        # effective provider, which defaults to `cline` and bills Cline Credits.
        if free_routing.enforcement_enabled():
            cmd.extend(["-P", free_routing.FREE_PROVIDER])
        # A concrete model is always sent. "auto" and any paid id are coerced,
        # because omitting -m hands the choice back to the drifted persisted state.
        target_model = free_routing.coerce_model(model or self._default_model)
        if target_model:
            cmd.extend(["-m", target_model])
        if opts.get("session_id_native"):
            cmd.extend(["--id", str(opts["session_id_native"])])
        if opts.get("mode") == "plan":
            cmd.append("--plan")
        if work_dir:
            cmd.extend(["--cwd", str(work_dir)])
        cmd.append(prompt)

        self._current_status = AgentStatus.WORKING
        start = time.time()
        import datetime
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(work_dir or Path.cwd()),
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            self._current_status = AgentStatus.FAILED
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return self._failure(
                task_id, f"Task timed out after {timeout_seconds}s", model, 124, time.time() - start, started_at, completed_at
            )
        except Exception as exc:
            self._current_status = AgentStatus.FAILED
            completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            return self._failure(task_id, str(exc), model, 1, time.time() - start, started_at, completed_at)

        completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        response, payload, json_valid, in_tok, out_tok, tot_tok, rep_model = self._extract_response(proc.stdout)
        stderr = proc.stderr.strip()
        success = proc.returncode == 0 and bool(response.strip())
        error = stderr if not success else ""
        if not success and not error:
            error = f"Cline produced no output (exit {proc.returncode})"

        usage_source = "reported" if (in_tok is not None or out_tok is not None) else "unknown"

        self._current_status = AgentStatus.IDLE if success else AgentStatus.FAILED
        return TaskExecutionResult(
            task_id=task_id,
            agent_id=self.agent_id,
            account_id=self.account_id,
            provider=self.provider,
            success=success,
            exit_code=proc.returncode,
            output=response,
            error=error,
            requested_model=target_model,
            actual_model=rep_model or UNKNOWN_MODEL,
            duration_seconds=time.time() - start,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            usage_source=usage_source,
            started_at=started_at,
            completed_at=completed_at,
            json_valid=json_valid,
            raw_response=payload,
            raw_stdout=proc.stdout,
            raw_stderr=proc.stderr,
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
            requested_model=model,
            actual_model=UNKNOWN_MODEL,
            duration_seconds=duration,
            started_at=started_at,
            completed_at=completed_at,
            usage_source="unknown",
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
        opts = dict(options or {})
        opts.setdefault("session_id_native", session_id)
        return self.execute(task_id, prompt, model, work_dir, timeout_seconds, opts)

    def cancel(self, task_id: str) -> bool:
        self._current_status = AgentStatus.IDLE
        return True
