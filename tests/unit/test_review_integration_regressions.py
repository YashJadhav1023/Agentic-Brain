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
