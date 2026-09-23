"""Regression tests for core correctness fixes found in the review/core pass.

Each test fails on the pre-fix code and passes after. Nothing here touches the
user's real config, credentials, keyring or agent CLIs: every path is a temp dir
and every collaborator that could reach the outside world is a mock.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_brain_cli():
    spec = importlib.util.spec_from_file_location(
        "_brain_cli_under_test", PROJECT_ROOT / "scripts" / "brain.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _partial_write_then_fail(self, data, *args, **kwargs):
    """Stand-in for Path.write_text that tears the file then fails (e.g. ENOSPC)."""
    with open(self, "w", encoding="utf-8") as fh:
        fh.write(data[: max(1, len(data) // 3)])
    raise OSError(28, "No space left on device")


class UndefinedNameRegressions(unittest.TestCase):
    def test_job_manager_dispatch_attempt_annotation_resolves(self):
        # F821: `-> ExecutionResult` named a class never imported in job_manager.
        from brain.orchestrator.job_manager import JobManager
        from agents.base.adapter import TaskExecutionResult

        hints = typing.get_type_hints(JobManager._dispatch_attempt)
        self.assertIs(hints["return"], TaskExecutionResult)

    def test_brain_cli_imports_any(self):
        # F821: cmd_doctor annotated `list[dict[str, Any]]` without importing Any.
        cli = _load_brain_cli()
        self.assertIs(getattr(cli, "Any", None), typing.Any)

    def test_resources_search_does_not_raise_name_error(self):
        # F821: `brain resources search` referenced an undefined `graph`.
        from brain.resources.resource_model import Resource, ResourceType

        cli = _load_brain_cli()
        rtype = next(iter(ResourceType))
        tagged = Resource(id="r1", type=rtype, name="Tagged Thing", location="/x", tags=["Docker", "ops"])
        other = Resource(id="r2", type=rtype, name="Other", location="/y", tags=["python"])

        fake_reg = mock.MagicMock()
        fake_reg.list.return_value = [tagged, other]
        with mock.patch("brain.resources.resource_registry.ResourceRegistry", return_value=fake_reg):
            args = argparse.Namespace(resources_command="search", query="docker", json=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_resources(args)
        found = json.loads(buf.getvalue())
        self.assertEqual([r["id"] for r in found], ["r1"])


class ShellExecutionGuardRegressions(unittest.TestCase):
    def test_rollback_action_is_safety_checked(self):
        # rollback_action runs through the shell but bypassed the guardrail.
        from brain.orchestrator.execution_fabric import ExecutionFabric
        from brain.planner.plan_model import PlanStep

        fabric = ExecutionFabric.__new__(ExecutionFabric)  # no registries needed
        step = PlanStep(
            step_id="s1",
            title="harmless step",
            command="true",
            rollback_action="rm -rf ~/.gemini",
        )
        with mock.patch("brain.orchestrator.execution_fabric.subprocess.run") as run:
            res = fabric.execute_step(step)
        self.assertEqual(res.status, "BLOCKED_SAFETY_VIOLATION")
        run.assert_not_called()

    def test_simulated_plan_failure_never_runs_rollback_command(self):
        # A task title containing "fatal:" failed the no_errors rule on the
        # *simulated* output and then ran the step's rollback ("git checkout .")
        # through the shell, discarding the user's uncommitted work.
        from brain.planner.plan_model import Plan, PlanStatus, PlanStep
        from brain.planner.planner import Planner
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "rollback-ran"
            planner = Planner.__new__(Planner)
            planner._lock = threading.RLock()
            planner.storage_dir = Path(tmp)
            step = PlanStep(
                step_id="step-2",
                title="Execute coding action: catch fatal: signals",
                verification_rules=["no_errors"],
                rollback_action=f"touch {sentinel}",
            )
            planner._plans = {
                "plan-x": Plan(plan_id="plan-x", task="t", domain="Coding", steps=[step], status=PlanStatus.READY)
            }
            planner.execute_plan("plan-x")
            self.assertFalse(sentinel.exists(), "simulated step must not run a real rollback")


class RouterRegressions(unittest.TestCase):
    def test_string_routing_mode_is_normalised(self):
        from brain.router.models import RoutingMode
        from brain.router.smart_router import SmartRouter

        with tempfile.TemporaryDirectory() as tmp:
            router = SmartRouter(registry=mock.MagicMock(), history_file=Path(tmp) / "h.jsonl")
            seen = {}

            def fake_score(task_text, complexity=None, routing_mode=RoutingMode.BALANCED, user_preference=None):
                seen["mode"] = routing_mode
                return []

            with mock.patch.object(router, "score_candidates", side_effect=fake_score):
                with self.assertRaises(RuntimeError):
                    router.route("summarise the readme", routing_mode="COST")
        self.assertIs(seen["mode"], RoutingMode.COST)


class SwarmRegressions(unittest.TestCase):
    def test_file_lock_failure_deregisters_active_task(self):
        from brain.orchestrator.swarm import SwarmWorkerPool
        from tasks.manager import TaskManager

        with tempfile.TemporaryDirectory() as tmp:
            tm = TaskManager(root_tasks_dir=Path(tmp) / "tasks", include_swarm=False)
            task = tm.create_task(title="edit file", description="d", assigned_agent="kiro-cli")
            task.files = ["src/a.py"]

            adapter = mock.MagicMock()
            adapter.agent_id = "kiro-cli"
            adapter.health.return_value = (True, "ok")
            registry = mock.MagicMock()
            registry.get_adapter.return_value = adapter
            locker = mock.MagicMock()
            locker.acquire.return_value = False

            pool = SwarmWorkerPool(
                task_manager=tm,
                registry=registry,
                event_bus=mock.MagicMock(),
                file_locker=locker,
                handoff_manager=mock.MagicMock(),
                memory_store=mock.MagicMock(),
                session_manager=mock.MagicMock(),
                workspace_dir=Path(tmp),
                worktree_manager=mock.MagicMock(),
                context_optimizer=mock.MagicMock(),
                token_tracker=mock.MagicMock(),
                failover_manager=mock.MagicMock(),
            )
            res = pool.execute_task(task)
            self.assertFalse(res.success)
            self.assertIn("Failed to acquire lock", res.error)
            self.assertFalse(pool.is_task_active(task.task_id))


class PersistenceRegressions(unittest.TestCase):
    def test_task_survives_failed_save(self):
        # save_task deleted the old copy before writing the new one, so a failed
        # serialisation/write lost the task entirely.
        from tasks.manager import Task, TaskManager, TaskStatus

        with tempfile.TemporaryDirectory() as tmp:
            tm = TaskManager(root_tasks_dir=Path(tmp), include_swarm=False)
            task = tm.create_task(title="keep me", description="d")
            task.status = TaskStatus.RUNNING
            with mock.patch.object(Task, "to_dict", side_effect=ValueError("boom")):
                with self.assertRaises(ValueError):
                    tm.save_task(task)
            survivor = tm.get_task(task.task_id)
            self.assertIsNotNone(survivor)
            self.assertEqual(survivor.title, "keep me")

    def test_task_write_is_atomic(self):
        from tasks.manager import TaskManager, TaskStatus

        with tempfile.TemporaryDirectory() as tmp:
            tm = TaskManager(root_tasks_dir=Path(tmp), include_swarm=False)
            task = tm.create_task(title="atomic", description="d")
            with mock.patch.object(pathlib.Path, "write_text", _partial_write_then_fail):
                try:
                    tm.update_status(task.task_id, TaskStatus.READY, error="x")
                except OSError:
                    pass
            reloaded = tm.get_task(task.task_id)
            self.assertIsNotNone(reloaded, "record must never be left torn")
            leftovers = [p.name for p in Path(tmp).rglob("*.tmp")]
            self.assertEqual(leftovers, [])

    def test_job_persist_is_atomic(self):
        from brain.orchestrator.job import Job
        from brain.orchestrator.job_manager import JobManager
        from providers.base import ProviderType

        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp)
            mgr = JobManager.__new__(JobManager)
            mgr._storage_dir = storage
            job = Job(id="job-abc", provider="p", provider_type=ProviderType("api"), account="a",
                      worker=None, model="m", task="t")
            mgr._persist_job(job)
            first = (storage / "job-abc.json").read_text(encoding="utf-8")
            with mock.patch.object(pathlib.Path, "write_text", _partial_write_then_fail):
                mgr._persist_job(job)
            self.assertEqual(json.loads((storage / "job-abc.json").read_text(encoding="utf-8")),
                             json.loads(first))
            self.assertEqual(list(storage.glob("*.tmp")), [])

    def test_save_config_uses_unique_temp_file(self):
        # A fixed "providers.tmp" was shared by every concurrent saver.
        from providers.registry import config as cfg

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "providers.json"
            target.write_text("{}", encoding="utf-8")
            (Path(tmp) / "providers.tmp").mkdir()  # occupy the old fixed name
            cfg.save_config({"providers": {"x": {}}}, str(target))
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"providers": {"x": {}}})
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])

    def test_encrypted_store_refuses_to_clobber_unreadable_store(self):
        from providers.registry.credential_manager import EncryptedFileStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.dat"
            store = EncryptedFileStore(storage_path=path)
            self.assertTrue(store.store("a", "secret-a"))
            raw = bytearray(path.read_bytes())
            raw[-1] ^= 0xFF  # tamper / corrupt
            path.write_bytes(bytes(raw))
            before = path.read_bytes()

            self.assertFalse(store.store("b", "secret-b"))
            self.assertEqual(path.read_bytes(), before, "other credentials must not be overwritten")
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))


if __name__ == "__main__":
    unittest.main()
