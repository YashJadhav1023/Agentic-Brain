"""Sentinel: the suite must not modify the checkout it runs in.

Named ``test_zz_*`` so discovery runs it last (``e2e`` < ``integration`` <
``unit``, and alphabetically last within ``unit``). It compares against the
snapshot tests/support/hermetic.py takes when the ``tests`` package is first
imported, before any test module loads.

Before the hermetic harness, a full run rewrote the tracked
handoffs/current.{md,json} (with a "task in rate limit" test fixture), created
sessions/session_registry.json, and wrote runtime/jobs, runtime/logs,
runtime/audit and memory/store/shared_memory.db in the live checkout.
"""
from __future__ import annotations

import os
import unittest

from tests.support import hermetic


class TestSuiteLeavesRepoStateUntouched(unittest.TestCase):
    def test_tracked_state_files_are_byte_identical(self):
        if not hermetic.REPO_SENTINEL:
            self.skipTest("hermetic harness not installed")
        now = hermetic.snapshot_protected_repo_files()
        changed = sorted(
            rel for rel, digest in hermetic.REPO_SENTINEL.items() if now.get(rel) != digest
        )
        self.assertEqual(
            changed,
            [],
            f"the test run modified tracked repo files: {changed}. A test is "
            "writing live state; redirect it (see tests/support/hermetic.py).",
        )

    def test_no_writes_into_mutable_repo_trees(self):
        """handoffs/, sessions/, memory/store/, runtime/, tasks/ stay untouched.

        A dashboard or swarm worker running from the same checkout legitimately
        writes these trees too, so this is a hard failure only when
        BRAIN_TESTS_STRICT_REPO_STATE=1 (CI, or a throwaway worktree); otherwise
        it skips and lists what changed.
        """
        if hermetic.STATE_ROOT is None:
            self.skipTest("repo-state redirection disabled (BRAIN_TESTS_LIVE_REPO_STATE=1)")
        before = hermetic.MUTABLE_TREE_SENTINEL
        after = hermetic.snapshot_mutable_repo_trees()
        changed = sorted(rel for rel, meta in after.items() if before.get(rel) != meta)
        if not changed:
            return
        message = f"files written under mutable repo trees during the run: {changed[:25]}"
        if os.environ.get("BRAIN_TESTS_STRICT_REPO_STATE", "").strip() == "1":
            self.fail(message)
        self.skipTest(message + " (set BRAIN_TESTS_STRICT_REPO_STATE=1 to fail)")


if __name__ == "__main__":
    unittest.main()
