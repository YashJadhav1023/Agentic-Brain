"""Office view and task-store synchronisation.

Two defects made the Office view disagree with the task list, and both are
pinned here:

1. The SSE watcher iterated ``in_progress`` while the swarm CLI writes
   ``in-progress``, so no ``task.started`` event ever fired and the Office view
   did not move when a task began running.
2. ``build_office_state`` added swarm records before Mission Control records and
   de-duplicated by id, so a stale swarm mirror outranked the authoritative MC
   record. Observed live: two tasks cancelled in Mission Control still showed as
   live work in the Office.

Hermetic: every case builds its own temporary brain root and in-memory task
objects. Nothing reads the developer's real ~/agentic-brain or starts a server.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from ui.dashboard import office_state


class _Redactor:
    """Identity redactor: these tests assert on structure, not on redaction."""

    def redact(self, text: str) -> str:
        return text


def _mc_task(task_id: str, status: str, title: str = "A task", agent: str = "kiro-cli") -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id,
        title=title,
        status=SimpleNamespace(value=status),
        assigned_agent=agent,
        assigned_account="cli",
        assigned_model="auto",
        actual_model=None,
        complexity="standard",
        requires_approval=False,
        created_at="2026-09-22T05:48:38+00:00",
        started_at="2026-09-22T05:48:38+00:00",
        completed_at="2026-09-22T05:49:00+00:00",
        duration_seconds=22.0,
        errors=[],
    )


def _write_swarm_task(root: Path, state: str, task_id: str, title: str = "A task") -> Path:
    folder = root / "swarm" / "tasks" / state
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{task_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": task_id,
                "title": title,
                "status": state,
                "created_at": "2026-09-22T05:48:38.773230+00:00",
                "started_at": "2026-09-22T05:48:38.918396+00:00",
                "updated_at": "2026-09-22T05:48:38.956215+00:00",
            }
        ),
        encoding="utf-8",
    )
    return path


class TestWatcherFolderNames(unittest.TestCase):
    """The watcher must use the directory names that exist on disk."""

    def test_watcher_states_match_the_real_directory_names(self) -> None:
        from ui.dashboard.dashboard import _SWARM_STATE_EVENTS

        # office_state.SWARM_STATES is the list of names the reader accepts. Every
        # name the watcher claims to watch must be one the reader recognises, or
        # the two halves of the same feature are looking at different paths --
        # which is exactly how "in_progress" survived.
        for state in _SWARM_STATE_EVENTS:
            self.assertIn(state, office_state.SWARM_STATES, f"{state} is not a known swarm state")

    def test_running_state_is_hyphenated(self) -> None:
        from ui.dashboard.dashboard import _SWARM_STATE_EVENTS

        self.assertIn("in-progress", _SWARM_STATE_EVENTS)
        self.assertNotIn("in_progress", _SWARM_STATE_EVENTS)
        self.assertEqual(_SWARM_STATE_EVENTS["in-progress"], "task.started")

    def test_every_lifecycle_state_is_watched(self) -> None:
        from ui.dashboard.dashboard import _SWARM_STATE_EVENTS

        for state in ("pending", "in-progress", "completed", "escalated"):
            self.assertIn(state, _SWARM_STATE_EVENTS)

    def test_watcher_directory_names_exist_under_a_real_swarm_root(self) -> None:
        """The names must resolve against a store laid out the way the CLI writes it."""
        from ui.dashboard.dashboard import _SWARM_STATE_EVENTS

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for state in ("pending", "in-progress", "completed", "escalated"):
                (root / "swarm" / "tasks" / state).mkdir(parents=True, exist_ok=True)
            for state in _SWARM_STATE_EVENTS:
                self.assertTrue(
                    (root / "swarm" / "tasks" / state).is_dir(),
                    f"watcher would watch a non-existent directory: {state}",
                )


class TestStorePrecedence(unittest.TestCase):
    """Mission Control's record wins when both stores know a task."""

    def _build(self, root: Path, mc_tasks: list) -> dict:
        return office_state.build_office_state(
            mc_tasks=mc_tasks,
            jobs=[],
            routing_history=[],
            memory_store=None,
            accounts=[],
            brain_root=root,
            redactor=_Redactor(),
        )

    def test_mission_control_status_wins_over_a_stale_swarm_mirror(self) -> None:
        """The regression: cancelled in MC, still in-progress in the swarm pool.

        The swarm record is kept, because it carries fields the MC record does not
        have (sandbox branch, attempt trail, provider duration). Only its stage is
        corrected, so the Office stops showing finished work as live.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_swarm_task(root, "in-progress", "task-32f08138", "Review one assertion")
            state = self._build(root, [_mc_task("task-32f08138", "CANCELLED", "Review one assertion")])

            entries = [t for t in state["tasks"] if t["id"] == "task-32f08138"]
            self.assertEqual(len(entries), 1, "the task must appear exactly once")
            # Reconciled, not replaced: the richer swarm record is still the row.
            self.assertEqual(entries[0]["source"], "swarm")
            # But it must no longer claim the task is live.
            self.assertEqual(entries[0]["stage"], "escalated")
            self.assertNotEqual(entries[0]["stage"], "running")

    def test_a_live_swarm_task_is_not_marked_finished_by_a_lagging_mc_record(self) -> None:
        """Reconciliation is one-directional.

        A task genuinely running in the swarm must not be closed just because
        Mission Control has not caught up, or the Office would hide active work.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_swarm_task(root, "in-progress", "task-live-both", "Still running")
            state = self._build(root, [_mc_task("task-live-both", "RUNNING", "Still running")])
            entry = next(t for t in state["tasks"] if t["id"] == "task-live-both")
            self.assertEqual(entry["stage"], "running")

    def test_a_task_only_the_swarm_knows_is_still_shown(self) -> None:
        """Precedence must not become exclusion: swarm-only tasks still appear."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_swarm_task(root, "in-progress", "task-swarm-only", "Swarm only")
            state = self._build(root, [_mc_task("task-other", "COMPLETED")])

            ids = {t["id"] for t in state["tasks"]}
            self.assertIn("task-swarm-only", ids)
            self.assertIn("task-other", ids)
            swarm_entry = next(t for t in state["tasks"] if t["id"] == "task-swarm-only")
            self.assertEqual(swarm_entry["source"], "swarm")

    def test_a_running_mission_control_task_reports_running(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = self._build(root, [_mc_task("task-live", "RUNNING")])
            entry = next(t for t in state["tasks"] if t["id"] == "task-live")
            self.assertEqual(entry["stage"], "running")

    def test_no_duplicate_ids_across_stores(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for tid in ("task-a", "task-b"):
                _write_swarm_task(root, "completed", tid)
            state = self._build(
                root, [_mc_task("task-a", "COMPLETED"), _mc_task("task-c", "FAILED")]
            )
            ids = [t["id"] for t in state["tasks"]]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate task ids: {ids}")

    def test_all_normalizers_share_one_key_shape(self) -> None:
        """The three sources feed one UI, so their dicts must stay identical.

        If they diverge, a task renders with missing fields depending only on
        which store it came from.
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_swarm_task(root, "completed", "task-swarm")
            state = self._build(root, [_mc_task("task-mc", "COMPLETED")])
            by_source = {t["source"]: set(t.keys()) for t in state["tasks"]}
            self.assertIn("mission-control", by_source)
            self.assertIn("swarm", by_source)
            self.assertEqual(by_source["mission-control"], by_source["swarm"])


if __name__ == "__main__":
    unittest.main()
