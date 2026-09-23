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



class HandoffTraversalRegressions(unittest.TestCase):
    def test_record_lookup_cannot_escape_archive(self):
        from handoffs.handoff_manager import HandoffManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            mgr = HandoffManager(root_dir=root)
            (Path(tmp) / "config").mkdir()
            (Path(tmp) / "config" / "providers.json").write_text('{"secret": 1}', encoding="utf-8")
            (root / "sibling.json").write_text('{"x": 1}', encoding="utf-8")
            for name in ("../../config/providers.json", "../sibling.json", "/etc/hostname.json",
                         "..\\sibling.json", ".."):
                self.assertIsNone(mgr.get_record_by_name(name), name)

            (root / "archive" / "handoff_1.json").write_text('{"ok": true}', encoding="utf-8")
            self.assertEqual(mgr.get_record_by_name("handoff_1.json"), {"ok": True})

    def test_symlink_out_of_archive_is_rejected(self):
        from handoffs.handoff_manager import HandoffManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "handoffs"
            mgr = HandoffManager(root_dir=root)
            outside = Path(tmp) / "outside.json"
            outside.write_text('{"leak": 1}', encoding="utf-8")
            (root / "archive" / "link.json").symlink_to(outside)
            self.assertIsNone(mgr.get_record_by_name("link.json"))


class CliCrashRegressions(unittest.TestCase):
    def _run_main(self, cli, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(cli.sys, "argv", ["brain.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_route_explain_tolerates_none_recommendations(self):
        cli = _load_brain_cli()
        router = mock.MagicMock()
        router.explain_routing.return_value = {
            "task": "t", "selected_agent": "a", "selected_account": "acc",
            "selected_provider": "p", "selected_model": "m", "task_type": "General",
            "complexity": "standard", "total_score": 1.0, "reason": "r",
            "recommended_knowledge": [None, "Doc A", None],
        }
        with mock.patch.object(cli, "create_default_registry"), \
                mock.patch.object(cli, "SmartRouter", return_value=router):
            code, out, _ = self._run_main(cli, ["route", "explain", "fix the build"])
        self.assertEqual(code, 0)
        self.assertIn("Recommended Docs:     Doc A", out)

    def test_explain_routing_drops_none_entries_at_source(self):
        from brain.router.smart_router import SmartRouter

        with tempfile.TemporaryDirectory() as tmp:
            router = SmartRouter(registry=mock.MagicMock(), history_file=Path(tmp) / "h.jsonl")
            decision = mock.MagicMock(agent_id="a", task_type="General", fallback_chain=[], candidates=[])
            ctx = mock.MagicMock(relevant_mcps=[{"x": 1}], relevant_tools=[{}],
                                 relevant_docs=[{"path": "no-title"}, {"title": "Doc"}],
                                 relevant_steering=[{}])
            with mock.patch.object(router, "route", return_value=decision), \
                    mock.patch("brain.context.context_builder.ContextBuilder.preview_context", return_value=ctx), \
                    mock.patch("brain.context.context_builder.ContextBuilder.__init__", return_value=None):
                expl = router.explain_routing("task")
        self.assertEqual(expl["recommended_knowledge"], ["Doc"])
        self.assertEqual(expl["recommended_mcps"], [])
        self.assertEqual(expl["recommended_tools"], [])

    def test_worktree_cleanup_without_confirm_is_a_clean_refusal(self):
        cli = _load_brain_cli()
        with mock.patch.object(cli, "Orchestrator") as orch:
            code, out, _ = self._run_main(cli, ["worktree", "cleanup"])
        self.assertEqual(code, 1)
        self.assertIn("--confirm", out)
        orch.return_value.worktrees.cleanup.assert_not_called()

    def test_worktree_cleanup_confirm_removes_only_stale_finished_sandboxes(self):
        import datetime as dt

        cli = _load_brain_cli()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)).isoformat()
        new = dt.datetime.now(dt.timezone.utc).isoformat()
        recs = [
            mock.MagicMock(task_id="old-rejected", status=mock.MagicMock(value="REJECTED"), updated_at=old),
            mock.MagicMock(task_id="old-review", status=mock.MagicMock(value="PENDING_REVIEW"), updated_at=old),
            mock.MagicMock(task_id="new-rejected", status=mock.MagicMock(value="REJECTED"), updated_at=new),
        ]
        with mock.patch.object(cli, "Orchestrator") as orch:
            wt = orch.return_value.worktrees
            wt.status.return_value = recs
            wt.cleanup.return_value = True
            code, out, _ = self._run_main(cli, ["worktree", "cleanup", "--confirm", "--max-age-hours", "24"])
        self.assertEqual(code, 0)
        wt.cleanup.assert_called_once_with("old-rejected")
        self.assertIn("Cleaned up 1", out)

    def test_route_with_no_agents_exits_cleanly(self):
        cli = _load_brain_cli()
        router = mock.MagicMock()
        router.route.side_effect = RuntimeError("No healthy agents available in ProviderRegistry")
        with mock.patch.object(cli, "create_default_registry"), \
                mock.patch.object(cli, "SmartRouter", return_value=router):
            code, _, err = self._run_main(cli, ["route", "fix the build"])
        self.assertEqual(code, 1)
        self.assertIn("Error: No healthy agents", err)


class PlannerNoAgentRegressions(unittest.TestCase):
    def _planner(self, tmp, router):
        import threading
        from brain.planner.planner import Planner
        from brain.planner.task_decomposer import TaskDecomposer
        from brain.router.classification import TaskClassifier

        planner = Planner.__new__(Planner)
        planner._lock = threading.RLock()
        planner.router = router
        planner.decomposer = TaskDecomposer(mock.MagicMock())
        planner.classifier = TaskClassifier()
        planner.storage_dir = Path(tmp)
        planner._plans = {}
        return planner

    def test_unregistered_default_agent_falls_back_to_open_routing(self):
        decision = mock.MagicMock(agent_id="kiro-cli", account_id="k", provider_id="kiro", model="m")

        def route(text, preferred_agent=None):
            if preferred_agent:
                raise RuntimeError(f"Requested agent '{preferred_agent}' is not registered.")
            return decision

        router = mock.MagicMock()
        router.route.side_effect = route
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._planner(tmp, router).create_plan("Summarise the architecture document")
        self.assertTrue(plan.steps)
        self.assertTrue(all(s.agent == "kiro-cli" for s in plan.steps))

    def test_no_agents_at_all_still_creates_a_plan(self):
        router = mock.MagicMock()
        router.route.side_effect = RuntimeError("No healthy agents available in ProviderRegistry")
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._planner(tmp, router).create_plan("Summarise the architecture document")
        self.assertTrue(plan.steps)
        self.assertIn("routing_warnings", plan.metadata)


class AuditPathRegressions(unittest.TestCase):
    def test_default_audit_path_is_repo_anchored_not_cwd_relative(self):
        from brain.governance.audit_logger import DEFAULT_AUDIT_LOG_PATH, AuditLogger

        # The module default is what production uses. The test harness
        # (tests/support/hermetic.py) redirects AuditLogger() instances into a
        # per-run temp dir, so assert the constant rather than an instance.
        self.assertEqual(DEFAULT_AUDIT_LOG_PATH, PROJECT_ROOT / "runtime" / "audit" / "audit.jsonl")

        with tempfile.TemporaryDirectory() as tmp:
            prev = os.getcwd()
            os.chdir(tmp)
            try:
                logger = AuditLogger()
            finally:
                os.chdir(prev)
            self.assertTrue(logger.log_path.is_absolute())
            self.assertFalse(logger.log_path.is_relative_to(Path(tmp).resolve()))
            self.assertFalse((Path(tmp) / "runtime").exists())

    def test_validate_scans_the_ledger_audit_logger_writes(self):
        import inspect

        cli = _load_brain_cli()
        src = inspect.getsource(cli.cmd_validate)
        self.assertNotIn('"runtime" / "logs" / "audit.jsonl"', src)
        self.assertIn("DEFAULT_AUDIT_LOG_PATH", src)


if __name__ == "__main__":
    unittest.main()
