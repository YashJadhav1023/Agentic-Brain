"""Regressions found while integrating the review branches."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from brain.analytics.usage_tracker import UsageTracker


class UsageRecordingRegressions(unittest.TestCase):
    """JobManager records usage with `job_id=`; that used to raise TypeError,
    which a bare except swallowed, so job usage and cost were never tracked."""

    def _tracker(self):
        tmp = tempfile.mkdtemp()
        return UsageTracker(storage_file=Path(tmp) / "usage.jsonl")

    def test_record_usage_accepts_job_id_keyword(self):
        tracker = self._tracker()
        rec = tracker.record_usage(
            job_id="job-1", provider_id="antigravity", account_id="antigravity-account-3",
            model_id="auto", success=True, total_tokens=42,
        )
        self.assertEqual(rec.job_id, "job-1")
        self.assertEqual(rec.total_tokens, 42)

    def test_record_usage_positional_id_still_works(self):
        rec = self._tracker().record_usage("job-2", provider_id="openai", total_tokens=5)
        self.assertEqual(rec.job_id, "job-2")

    def test_record_usage_without_any_id_is_an_error(self):
        with self.assertRaises(TypeError):
            self._tracker().record_usage(provider_id="openai")


if __name__ == "__main__":
    unittest.main()


class TodayIsReallyTodayRegressions(unittest.TestCase):
    """The Overview's "Tokens today" and cost fell back to all-time totals
    whenever nothing had run today, showing lifetime usage as today's."""

    def test_token_metrics_can_be_limited_to_one_day(self):
        import json
        from brain.context.context_optimizer import TokenTelemetryTracker

        log = Path(tempfile.mkdtemp()) / "token_telemetry.jsonl"
        base = {"task_id": "t", "agent_id": "a", "account_id": "a", "provider": "p",
                "requested_model": None, "actual_model": "m", "status": "known"}
        rows = [dict(base, total_tokens=1000, timestamp="2026-09-01T10:00:00+00:00"),
                dict(base, total_tokens=7, timestamp="2026-09-24T10:00:00+00:00")]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tracker = TokenTelemetryTracker(log_path=log)
        self.assertEqual(tracker.get_metrics()["known_tokens"], 1007)
        self.assertEqual(tracker.get_metrics(day="2026-09-24")["known_tokens"], 7)
        self.assertEqual(tracker.get_metrics(day="2026-09-23")["known_tokens"], 0)

    def test_overview_does_not_report_lifetime_usage_as_today(self):
        from unittest import mock
        from ui.dashboard import dashboard

        class FakeUsage:
            def get_summary(self, day=None, **_):
                if day:
                    return {"total_tokens": 0, "estimated_cost_usd": 0.0, "requests": 0}
                return {"total_tokens": 5000, "estimated_cost_usd": 102.0, "requests": 5}

        real_metrics = dashboard.orchestrator.swarm.token_tracker.get_metrics

        def fake_metrics(day=None):
            m = dict(real_metrics())
            m["known_tokens"] = 0 if day else 1_656_515
            return m

        with mock.patch.object(dashboard, "get_usage_tracker", return_value=FakeUsage()), \
             mock.patch.object(dashboard.orchestrator.swarm.token_tracker, "get_metrics", side_effect=fake_metrics):
            if hasattr(dashboard, "_invalidate_system_status_cache"):
                dashboard._invalidate_system_status_cache()
            handler = dashboard.MissionControlHandler.__new__(dashboard.MissionControlHandler)
            mc = handler._get_overview()["mission_control"]
        self.assertEqual(mc["today_tokens"], 0)
        self.assertEqual(mc["today_cost"], 0.0)
        self.assertEqual(mc["total_cost"], 102.0)
