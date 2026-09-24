"""pxpipe integration: a local token-compressing proxy for Claude traffic.

pxpipe (https://github.com/teamchong/pxpipe, MIT) is a loopback HTTP proxy that
rewrites the bulky parts of an Anthropic request — the static system prompt and
tool-doc slab, large tool results, and older history turns — into dense PNG pages
before the request leaves the machine. An image's token cost is set by its pixel
dimensions rather than by how much text it contains, so token-dense context gets
substantially cheaper. Recent turns, the user's own messages and the model's
response are never touched.

This module owns the proxy's lifecycle for the brain and nothing else. It does
not transform requests itself and it never handles a credential: agents point
``ANTHROPIC_BASE_URL`` at the proxy and their own CLI supplies its own auth,
which pxpipe forwards unchanged.

Two properties are enforced here because this runs on contributors' machines:

  * **Loopback only.** The proxy is started bound to 127.0.0.1. A non-loopback
    bind would expose an unauthenticated endpoint that forwards whatever
    credential it is handed to Anthropic.
  * **Lossy by design, so it is opt-in.** Exact strings inside imaged content can
    come back as plausible-but-wrong text rather than an error. Accounts enable
    it per account via ``use_pxpipe``, never globally by default.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Upstream's default port. Kept identical so a proxy a contributor already
#: started by hand is discovered rather than duplicated.
DEFAULT_PORT = 47821

#: Loopback, and not configurable. See the module docstring.
BIND_HOST = "127.0.0.1"

#: Where pxpipe writes its own per-request event log.
DEFAULT_EVENTS_LOG = Path.home() / ".pxpipe" / "events.jsonl"

_READY_TIMEOUT_SECONDS = 25.0
_READY_POLL_SECONDS = 0.25
_STOP_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class PxpipeStatus:
    """Observed state of the proxy. Safe to serialize; carries no credential."""

    running: bool
    base_url: str
    port: int
    reason: str
    pid: int | None = None
    source: str = "unknown"
    version: str | None = None
    managed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "base_url": self.base_url,
            "port": self.port,
            "reason": self.reason,
            "pid": self.pid,
            "source": self.source,
            "version": self.version,
            "managed": self.managed,
        }


@dataclass(frozen=True)
class SavingsReport:
    """Measured token savings, aggregated from pxpipe's own event log.

    ``measured_requests`` counts only rows where **both** halves of the
    comparison exist: pxpipe successfully probed the pre-compression baseline,
    *and* the upstream actually billed the compressed request. Rows missing
    either half are counted separately and excluded.

    That second condition is not pedantry. A request that failed upstream (a 429
    quota wall, say) still carries a valid baseline but has no usage block, so
    counting it would compare a real baseline against zero billed tokens and
    report a fictitious 100% saving. Verified against a live 429 row.
    """

    events_file: Path
    total_rows: int = 0
    measured_requests: int = 0
    unmeasured_requests: int = 0
    unbilled_requests: int = 0
    compressed_requests: int = 0
    baseline_tokens: int = 0
    billed_input_tokens: int = 0
    images_emitted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def saved_tokens(self) -> int:
        """Baseline minus billed input tokens over measured rows only."""
        return max(0, self.baseline_tokens - self.billed_input_tokens)

    @property
    def saved_pct(self) -> float:
        """Percentage of baseline input tokens avoided, over measured rows."""
        if self.baseline_tokens <= 0:
            return 0.0
        return round(100.0 * self.saved_tokens / self.baseline_tokens, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events_file": str(self.events_file),
            "total_rows": self.total_rows,
            "measured_requests": self.measured_requests,
            "unmeasured_requests": self.unmeasured_requests,
            "unbilled_requests": self.unbilled_requests,
            "compressed_requests": self.compressed_requests,
            "baseline_tokens": self.baseline_tokens,
            "billed_input_tokens": self.billed_input_tokens,
            "saved_tokens": self.saved_tokens,
            "saved_pct": self.saved_pct,
            "images_emitted": self.images_emitted,
            "errors": self.errors,
        }


class PxpipeManager:
    """Locate, start, stop and report on the local pxpipe proxy."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        repo_dir: str | Path | None = None,
        executable: str | None = None,
        runtime_dir: str | Path | None = None,
        events_file: str | Path | None = None,
        models: str | None = None,
    ) -> None:
        self._port = int(port)
        self._repo_dir = Path(str(repo_dir)).expanduser() if repo_dir else None
        self._executable = executable
        self._runtime_dir = (
            Path(str(runtime_dir)).expanduser()
            if runtime_dir
            else Path(__file__).resolve().parents[2] / "runtime"
        )
        self._events_file = (
            Path(str(events_file)).expanduser() if events_file else DEFAULT_EVENTS_LOG
        )
        self._models = models

    # --- Addresses and paths ---------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        """The value agents put in ``ANTHROPIC_BASE_URL``."""
        return f"http://{BIND_HOST}:{self._port}"

    @property
    def events_file(self) -> Path:
        return self._events_file

    @property
    def pid_file(self) -> Path:
        return self._runtime_dir / f"pxpipe-{self._port}.pid"

    @property
    def log_file(self) -> Path:
        return self._runtime_dir / f"pxpipe-{self._port}.log"

    # --- Discovery --------------------------------------------------------

    def resolve_launch_command(self) -> tuple[list[str], str] | None:
        """Return the argv that starts the proxy, and a label for where it came from.

        Preference order is deliberate: a checkout the operator controls beats a
        global install, which beats ``npx`` fetching from the network. The npx
        fallback is last because an unpinned network fetch at task time is the
        least predictable of the three.
        """
        if self._executable:
            explicit = shutil.which(self._executable) or str(Path(self._executable).expanduser())
            if Path(explicit).is_file() and os.access(explicit, os.X_OK):
                return [explicit], f"executable:{explicit}"

        if self._repo_dir:
            bundle = self._repo_dir / "dist" / "node.js"
            if bundle.is_file():
                node = shutil.which("node")
                if node:
                    return [node, str(bundle)], f"repo:{self._repo_dir}"

        for name in ("pxpipe", "pxpipe-proxy"):
            found = shutil.which(name)
            if found:
                return [found], f"path:{found}"

        npx = shutil.which("npx")
        if npx:
            # Pinned rather than floating: an unpinned `npx pxpipe-proxy` would
            # silently change the proxy's behaviour under a running swarm.
            return [npx, "--yes", "pxpipe-proxy@0.13.2"], "npx:pxpipe-proxy@0.13.2"
        return None

    def is_installed(self) -> bool:
        return self.resolve_launch_command() is not None

    # --- Liveness ---------------------------------------------------------

    def _port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((BIND_HOST, self._port)) == 0

    def _probe(self, timeout: float = 2.0) -> tuple[bool, str]:
        """Ask the proxy's own stats endpoint whether it is alive."""
        url = f"{self.base_url}/proxy-stats"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
                if response.status != 200:
                    return False, f"{url} returned HTTP {response.status}"
                json.loads(response.read().decode("utf-8") or "{}")
                return True, "proxy-stats responded"
        except urllib.error.HTTPError as exc:
            # An HTTP error still proves something is listening and speaking HTTP.
            return True, f"listening (HTTP {exc.code} on /proxy-stats)"
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            return False, f"no response on {url} ({exc.__class__.__name__})"
        except json.JSONDecodeError:
            return True, "listening (non-JSON /proxy-stats body)"

    def _read_pid(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            # Exists but belongs to another user: report it rather than claiming
            # nothing is running, otherwise start() would fight over the port.
            return pid
        return pid

    def status(self) -> PxpipeStatus:
        """Current proxy state, whether or not this process started it."""
        pid = self._read_pid()
        alive, reason = self._probe()
        if not alive and self._port_open():
            alive, reason = True, f"port {self._port} is open but /proxy-stats did not answer"
        launch = self.resolve_launch_command()
        return PxpipeStatus(
            running=alive,
            base_url=self.base_url,
            port=self._port,
            reason=reason if alive else (reason if launch else "pxpipe is not installed"),
            pid=pid,
            source=launch[1] if launch else "not-installed",
            managed=pid is not None,
        )

    # --- Lifecycle --------------------------------------------------------

    def start(self, wait_seconds: float = _READY_TIMEOUT_SECONDS) -> PxpipeStatus:
        """Start the proxy if it is not already up, and wait until it answers.

        Idempotent: an already-running proxy (including one a contributor started
        by hand) is adopted rather than duplicated, because two proxies on one
        port is a confusing failure and the second would simply fail to bind.
        """
        existing = self.status()
        if existing.running:
            return existing

        launch = self.resolve_launch_command()
        if not launch:
            return PxpipeStatus(
                running=False,
                base_url=self.base_url,
                port=self._port,
                reason="pxpipe is not installed: no checkout, no pxpipe on PATH, and no npx",
                source="not-installed",
            )

        argv, source = launch
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["PORT"] = str(self._port)
        env["HOST"] = BIND_HOST
        if self._models:
            env["PXPIPE_MODELS"] = self._models
        env["PXPIPE_LOG"] = str(self._events_file)

        with self.log_file.open("a", encoding="utf-8") as log:
            log.write(f"\n=== pxpipe start {time.strftime('%Y-%m-%dT%H:%M:%S%z')} via {source} ===\n")
            log.flush()
            process = subprocess.Popen(  # noqa: S603 - argv is resolved, never shell-interpolated
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(self._repo_dir) if self._repo_dir and self._repo_dir.is_dir() else None,
                start_new_session=True,
            )

        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if process.poll() is not None:
                self.pid_file.unlink(missing_ok=True)
                return PxpipeStatus(
                    running=False,
                    base_url=self.base_url,
                    port=self._port,
                    reason=f"pxpipe exited with code {process.returncode}; see {self.log_file}",
                    source=source,
                )
            alive, reason = self._probe()
            if alive:
                return PxpipeStatus(
                    running=True,
                    base_url=self.base_url,
                    port=self._port,
                    reason=reason,
                    pid=process.pid,
                    source=source,
                    managed=True,
                )
            time.sleep(_READY_POLL_SECONDS)

        return PxpipeStatus(
            running=False,
            base_url=self.base_url,
            port=self._port,
            reason=f"pxpipe did not answer within {wait_seconds:.0f}s; see {self.log_file}",
            pid=process.pid,
            source=source,
            managed=True,
        )

    def stop(self) -> tuple[bool, str]:
        """Stop a proxy this brain started. Never kills one it did not start."""
        pid = self._read_pid()
        if pid is None:
            if self.status().running:
                return False, (
                    f"a proxy is listening on {self.base_url} but was not started by the brain; "
                    "stop it where it was started"
                )
            self.pid_file.unlink(missing_ok=True)
            return True, "pxpipe was not running"

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self.pid_file.unlink(missing_ok=True)
            return True, f"pxpipe pid {pid} was already gone"
        except PermissionError:
            return False, f"pxpipe pid {pid} belongs to another user; not killing it"

        deadline = time.time() + _STOP_GRACE_SECONDS
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                self.pid_file.unlink(missing_ok=True)
                return True, f"pxpipe pid {pid} stopped"
            time.sleep(_READY_POLL_SECONDS)

        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.pid_file.unlink(missing_ok=True)
        return True, f"pxpipe pid {pid} did not exit on SIGTERM and was killed"

    # --- Measurement ------------------------------------------------------

    def savings(self, limit: int | None = None) -> SavingsReport:
        """Aggregate measured savings from pxpipe's event log.

        Reads the log rather than the live dashboard so the figure is available
        after the proxy has stopped. Malformed lines are counted as errors and
        skipped: a truncated final line is normal for an append-only log being
        written concurrently, and must not make the whole report fail.
        """
        path = self._events_file
        if not path.is_file():
            return SavingsReport(events_file=path, errors=[f"no event log at {path}"])

        total = measured = unmeasured = compressed = 0
        unbilled = 0
        baseline_sum = billed_sum = images = 0
        malformed = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError as exc:
            return SavingsReport(events_file=path, errors=[f"{exc.__class__.__name__}: {exc}"])

        if limit is not None and limit > 0:
            lines = lines[-limit:]

        for line in lines:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(row, dict):
                malformed += 1
                continue

            if row.get("compressed"):
                compressed += 1
            count = row.get("image_count")
            if isinstance(count, (int, float)):
                images += int(count)

            baseline = row.get("baseline_tokens")
            probe_ok = row.get("baseline_probe_status") == "ok"
            if not probe_ok or not isinstance(baseline, (int, float)):
                unmeasured += 1
                continue

            # The compressed side must have been billed for the comparison to
            # mean anything. A 429 has a baseline but no usage block; treating
            # its absent usage as zero would report a 100% saving on a request
            # that never ran.
            billed = 0
            billed_fields = 0
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                value = row.get(key)
                if isinstance(value, (int, float)):
                    billed += int(value)
                    billed_fields += 1

            status = row.get("status")
            http_ok = not isinstance(status, (int, float)) or 200 <= int(status) < 300
            if billed_fields == 0 or billed <= 0 or not http_ok:
                unbilled += 1
                continue

            measured += 1
            baseline_sum += int(baseline)
            billed_sum += billed

        errors = [f"{malformed} malformed line(s) skipped"] if malformed else []
        return SavingsReport(
            events_file=path,
            total_rows=total,
            measured_requests=measured,
            unmeasured_requests=unmeasured,
            unbilled_requests=unbilled,
            compressed_requests=compressed,
            baseline_tokens=baseline_sum,
            billed_input_tokens=billed_sum,
            images_emitted=images,
            errors=errors,
        )

    # --- Config-driven construction --------------------------------------

    @classmethod
    def from_config(cls, config_path: str | Path | None = None) -> PxpipeManager:
        """Build a manager from the ``integrations.pxpipe`` block in providers.json."""
        from providers.registry.config import load_config

        try:
            config = load_config(str(config_path) if config_path else None)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        block = config.get("integrations", {}).get("pxpipe", {})
        if not isinstance(block, dict):
            return cls()
        return cls(
            port=int(block.get("port", DEFAULT_PORT)),
            repo_dir=block.get("repo_dir"),
            executable=block.get("executable"),
            events_file=block.get("events_file"),
            models=block.get("models"),
        )

    @classmethod
    def enabled_in_config(cls, config_path: str | Path | None = None) -> bool:
        """Whether the integration block is enabled at all."""
        from providers.registry.config import load_config

        try:
            config = load_config(str(config_path) if config_path else None)
        except (FileNotFoundError, json.JSONDecodeError):
            return False
        block = config.get("integrations", {}).get("pxpipe", {})
        return bool(isinstance(block, dict) and block.get("enabled"))
