"""Tests for the pxpipe integration manager.

Hermetic: no test starts a real proxy, binds a port, or reaches the network.
Event logs are synthetic files, and the rows used are the shapes pxpipe actually
writes (observed in ~/.pxpipe/events.jsonl), including the 429 row that exposed
the savings bug this module now guards against.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from integrations.pxpipe import BIND_HOST, DEFAULT_PORT, PxpipeManager

#: A compressed request that was actually billed: both halves of the comparison
#: exist, so this row is measurable.
MEASURED_ROW = {
    "path": "/v1/messages",
    "status": 200,
    "model": "claude-sonnet-5",
    "compressed": True,
    "orig_chars": 116185,
    "outgoing_text_chars": 30958,
    "image_count": 16,
    "baseline_tokens": 40000,
    "baseline_probe_status": "ok",
    "input_tokens": 8000,
    "cache_creation_input_tokens": 1000,
    "cache_read_input_tokens": 1000,
}

#: Observed live: a quota wall. It has a valid baseline but no billed usage, so
#: counting it would report a fictitious 100% saving.
RATE_LIMITED_ROW = {
    "path": "/v1/messages",
    "status": 429,
    "model": "claude-sonnet-5",
    "compressed": True,
    "image_count": 16,
    "baseline_tokens": 46754,
    "baseline_probe_status": "ok",
}

#: A request pxpipe forwarded without probing a baseline.
UNMEASURED_ROW = {
    "path": "/v1/messages",
    "status": 200,
    "compressed": False,
    "baseline_probe_status": "failed",
    "input_tokens": 500,
}


def _write_log(directory: Path, rows: list[dict], trailing: str = "") -> Path:
    path = directory / "events.jsonl"
    body = "".join(json.dumps(row) + "\n" for row in rows) + trailing
    path.write_text(body, encoding="utf-8")
    return path


class TestAddressing(unittest.TestCase):
    def test_base_url_is_loopback_only(self) -> None:
        """A non-loopback bind would expose an unauthenticated forwarder."""
        self.assertEqual(BIND_HOST, "127.0.0.1")
        self.assertEqual(PxpipeManager().base_url, f"http://127.0.0.1:{DEFAULT_PORT}")

    def test_port_is_honoured(self) -> None:
        self.assertEqual(PxpipeManager(port=9999).base_url, "http://127.0.0.1:9999")

    def test_pid_and_log_files_are_port_scoped(self) -> None:
        """Two managers on different ports must not fight over one pid file."""
        with TemporaryDirectory() as tmp:
            a = PxpipeManager(port=1111, runtime_dir=tmp)
            b = PxpipeManager(port=2222, runtime_dir=tmp)
            self.assertNotEqual(a.pid_file, b.pid_file)
            self.assertNotEqual(a.log_file, b.log_file)


class TestLaunchResolution(unittest.TestCase):
    def test_a_checkout_is_preferred_over_the_network(self) -> None:
        with TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "dist"
            bundle.mkdir()
            (bundle / "node.js").write_text("// bundle", encoding="utf-8")
            resolved = PxpipeManager(repo_dir=tmp).resolve_launch_command()
            self.assertIsNotNone(resolved)
            assert resolved is not None
            argv, source = resolved
            self.assertTrue(source.startswith("repo:"))
            self.assertEqual(argv[-1], str(bundle / "node.js"))

    def test_npx_fallback_is_version_pinned(self) -> None:
        """An unpinned npx fetch would change proxy behaviour under a live swarm."""
        resolved = PxpipeManager(repo_dir="/nonexistent-path-xyz").resolve_launch_command()
        if resolved is None:
            self.skipTest("neither pxpipe nor npx is installed on this host")
        argv, source = resolved
        if source.startswith("npx:"):
            self.assertTrue(any("@0.13.2" in part for part in argv), argv)

    def test_missing_install_is_reported_not_raised(self) -> None:
        manager = PxpipeManager(port=1, repo_dir="/nonexistent-path-xyz", executable="no-such-binary-xyz")
        # Either a real pxpipe/npx exists on this host, or nothing does; both are
        # valid outcomes and neither may raise.
        self.assertIn(manager.is_installed(), (True, False))


class TestSavings(unittest.TestCase):
    def test_missing_log_is_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            report = PxpipeManager(events_file=Path(tmp) / "absent.jsonl").savings()
            self.assertEqual(report.measured_requests, 0)
            self.assertTrue(report.errors)

    def test_measured_row_is_aggregated(self) -> None:
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [MEASURED_ROW])
            report = PxpipeManager(events_file=log).savings()
            self.assertEqual(report.measured_requests, 1)
            self.assertEqual(report.baseline_tokens, 40000)
            self.assertEqual(report.billed_input_tokens, 10000)
            self.assertEqual(report.saved_tokens, 30000)
            self.assertEqual(report.saved_pct, 75.0)
            self.assertEqual(report.images_emitted, 16)
            self.assertEqual(report.compressed_requests, 1)

    def test_failed_request_cannot_inflate_savings(self) -> None:
        """The regression this module was fixed for.

        A 429 has a baseline and no usage. Counting it produced "100% saved" on
        a request that never ran.
        """
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [RATE_LIMITED_ROW])
            report = PxpipeManager(events_file=log).savings()
            self.assertEqual(report.measured_requests, 0)
            self.assertEqual(report.unbilled_requests, 1)
            self.assertEqual(report.saved_tokens, 0)
            self.assertEqual(report.saved_pct, 0.0)

    def test_unprobed_request_is_excluded_not_counted_as_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [UNMEASURED_ROW])
            report = PxpipeManager(events_file=log).savings()
            self.assertEqual(report.measured_requests, 0)
            self.assertEqual(report.unmeasured_requests, 1)
            self.assertEqual(report.saved_pct, 0.0)

    def test_mixed_log_counts_each_class_separately(self) -> None:
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [MEASURED_ROW, RATE_LIMITED_ROW, UNMEASURED_ROW, MEASURED_ROW])
            report = PxpipeManager(events_file=log).savings()
            self.assertEqual(report.total_rows, 4)
            self.assertEqual(report.measured_requests, 2)
            self.assertEqual(report.unbilled_requests, 1)
            self.assertEqual(report.unmeasured_requests, 1)
            self.assertEqual(report.baseline_tokens, 80000)
            self.assertEqual(report.saved_pct, 75.0)

    def test_truncated_final_line_is_skipped_not_fatal(self) -> None:
        """An append-only log being written concurrently ends mid-row sometimes."""
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [MEASURED_ROW], trailing='{"path":"/v1/mess')
            report = PxpipeManager(events_file=log).savings()
            self.assertEqual(report.measured_requests, 1)
            self.assertTrue(any("malformed" in e for e in report.errors))

    def test_limit_considers_only_recent_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [MEASURED_ROW, MEASURED_ROW, RATE_LIMITED_ROW])
            report = PxpipeManager(events_file=log).savings(limit=1)
            self.assertEqual(report.total_rows, 1)
            self.assertEqual(report.measured_requests, 0)

    def test_report_is_json_serializable(self) -> None:
        with TemporaryDirectory() as tmp:
            log = _write_log(Path(tmp), [MEASURED_ROW])
            payload = json.dumps(PxpipeManager(events_file=log).savings().to_dict())
            self.assertIn("saved_pct", payload)


class TestConfigBinding(unittest.TestCase):
    def test_reads_the_integration_block(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "providers.json"
            config.write_text(
                json.dumps(
                    {
                        "providers": {},
                        "integrations": {
                            "pxpipe": {
                                "enabled": True,
                                "port": 45678,
                                "events_file": str(Path(tmp) / "e.jsonl"),
                                "models": "claude-fable-5",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(PxpipeManager.enabled_in_config(config))
            manager = PxpipeManager.from_config(config)
            self.assertEqual(manager.port, 45678)
            self.assertEqual(manager.base_url, "http://127.0.0.1:45678")

    def test_absent_block_means_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            config = Path(tmp) / "providers.json"
            config.write_text(json.dumps({"providers": {}}), encoding="utf-8")
            self.assertFalse(PxpipeManager.enabled_in_config(config))

    def test_missing_config_file_does_not_raise(self) -> None:
        missing = Path("/nonexistent-dir-xyz/providers.json")
        self.assertFalse(PxpipeManager.enabled_in_config(missing))
        self.assertEqual(PxpipeManager.from_config(missing).port, DEFAULT_PORT)


class TestStop(unittest.TestCase):
    def test_stopping_a_dead_proxy_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            # Port 1 is privileged and never has our proxy on it, so status() is
            # unambiguously "not running" without binding anything.
            ok, reason = PxpipeManager(port=1, runtime_dir=tmp).stop()
            self.assertTrue(ok)
            self.assertIn("not running", reason)

    def test_a_stale_pid_file_is_cleaned_up(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = PxpipeManager(port=1, runtime_dir=tmp)
            manager.pid_file.parent.mkdir(parents=True, exist_ok=True)
            manager.pid_file.write_text("999999999", encoding="utf-8")
            ok, _ = manager.stop()
            self.assertTrue(ok)
            self.assertFalse(manager.pid_file.exists())


if __name__ == "__main__":
    unittest.main()
