"""Phase 6B Runtime Truth, Real Provider Execution, Telemetry & State Reconciliation Tests."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.base.adapter import AgentStatus, ExecutionResult, TaskExecutionResult
from agents.cline.adapter import ClineAdapter
from agents.kiro.adapter import KiroAdapter
from agents.antigravity.adapter import AntigravityAdapter
from brain.context.context_optimizer import TokenRecord, TokenTelemetryTracker
from events.bus import Event, EventBus, EventType
from handoffs.handoff_manager import HandoffManager, HandoffRecord
from memory.retrieval.retriever import MemoryRetriever
from memory.store.memory_store import MemoryEntry, MemoryScope, MemoryStore
from providers.registry.bootstrap import create_default_registry
from tasks.manager import Task, TaskManager, TaskPriority, TaskStatus


class TestPhase6BExecutionResultContract(unittest.TestCase):
    """Test common ExecutionResult contract across all adapters."""

    def test_execution_result_shape(self):
        res = TaskExecutionResult(
            task_id="task-test-01",
            agent_id="test-agent",
            account_id="test-account",
            success=True,
            output="Done successfully",
            error="",
            exit_code=0,
            started_at="2026-09-07T12:00:00Z",
            completed_at="2026-09-07T12:00:05Z",
            duration_seconds=5.0,
            requested_model="test-model",
            actual_model="test-model",
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            usage_source="reported",
            provider="test-provider",
        )
        contract = res.to_execution_result()

        self.assertIsInstance(contract, dict)
        self.assertEqual(contract["task_id"], "task-test-01")
        self.assertEqual(contract["provider"], "test-provider")
        self.assertEqual(contract["agent_id"], "test-agent")
        self.assertEqual(contract["model"], "test-model")
        self.assertEqual(contract["status"], "completed")
        self.assertEqual(contract["exit_code"], 0)
        self.assertEqual(contract["started_at"], "2026-09-07T12:00:00Z")
        self.assertEqual(contract["completed_at"], "2026-09-07T12:00:05Z")
        self.assertEqual(contract["duration_seconds"], 5.0)
        self.assertEqual(contract["stdout"], "Done successfully")
        self.assertEqual(contract["usage"]["input_tokens"], 100)
        self.assertEqual(contract["usage"]["output_tokens"], 50)
        self.assertEqual(contract["usage"]["total_tokens"], 150)
        self.assertEqual(contract["usage"]["usage_source"], "reported")
        self.assertIsNone(contract["error"])

    def test_kiro_adapter_honest_unknown_tokens(self):
        """Kiro adapter must report usage_source='unknown' and null tokens when not exposed."""
        adapter = KiroAdapter(executable="kiro-cli")
        mock_proc = MagicMock(returncode=0, stdout="Kiro completed work", stderr="")
        with patch.object(adapter, "_resolve_executable", return_value="/bin/kiro-cli"), \
             patch("subprocess.run", return_value=mock_proc):
            result = adapter.execute(task_id="t-kiro", prompt="Analyze repo")
            contract = result.to_execution_result()

            self.assertEqual(contract["provider"], "kiro")
            self.assertEqual(contract["agent_id"], "kiro-cli")
            self.assertEqual(contract["usage"]["usage_source"], "unknown")
            self.assertIsNone(contract["usage"]["input_tokens"])
            self.assertIsNone(contract["usage"]["output_tokens"])
            self.assertIsNone(contract["usage"]["total_tokens"])

    def test_cline_adapter_token_extraction(self):
        """Cline adapter extracts real token telemetry from NDJSON stream."""
        adapter = ClineAdapter(executable="cline")
        ndjson_output = (
            '{"type":"say","say":"text","text":"Started execution"}\n'
            '{"type":"say","say":"command","text":"ls -la"}\n'
            '{"type":"run_result","command":"ls -la","output":"total 4"}\n'
            '{"type":"agent_event","usage":{"inputTokens":12500,"outputTokens":850}}\n'
            '{"type":"say","say":"completion_result","text":"Task complete"}\n'
        )
        mock_proc = MagicMock(returncode=0, stdout=ndjson_output, stderr="")
        with patch.object(adapter, "_resolve_executable", return_value="/bin/cline"), \
             patch("subprocess.run", return_value=mock_proc):
            result = adapter.execute(task_id="t-cline", prompt="Inspect codebase")
            contract = result.to_execution_result()

            self.assertEqual(contract["provider"], "cline")
            self.assertEqual(contract["usage"]["input_tokens"], 12500)
            self.assertEqual(contract["usage"]["output_tokens"], 850)
            self.assertEqual(contract["usage"]["total_tokens"], 13350)
            self.assertEqual(contract["usage"]["usage_source"], "reported")
            self.assertEqual(contract["status"], "completed")

    def test_cline_free_model_configuration(self):
        """Inspect provider configuration: Cline declares free models."""
        registry = create_default_registry()
        # BUG-005: cline accounts are registered individually.
        cline = registry.get_adapter("cline-account-1")
        self.assertIsNotNone(cline)
        # `deepseek/deepseek-v4-flash` and `auto` were previously asserted here as
        # Cline's free models. Neither is free: on the `cline` provider deepseek
        # spends Cline Credits ($0.00 on this account) and OpenRouter's `:free`
        # slugs now answer "This model is unavailable for free", while `auto`
        # delegates the choice back to Cline's mutable persisted state. The adapter
        # now offers only the verified free Gemini route.
        from agents.cline import free_routing

        self.assertTrue(cline.models, "cline must advertise at least one model")
        for model_id in cline.models:
            self.assertTrue(
                free_routing.is_free_model(model_id),
                f"cline advertises non-free model {model_id!r}",
            )

        # config/providers.json still lists paid ids (anthropic/claude-fable-5.1,
        # z-ai/glm-5.3-flash) under cline.model_capabilities, some flagged
        # billing="free". That stale data is a separate fix; what matters for cost is
        # that the adapter refuses to *offer* them regardless of what the file says.
        providers_data = json.loads(Path("config/providers.json").read_text(encoding="utf-8"))
        declared = [m["id"] for m in providers_data["providers"]["cline"]["model_capabilities"]]
        leaked = [m for m in declared if m in cline.models and not free_routing.is_free_model(m)]
        self.assertEqual(leaked, [], f"paid declared models reached the adapter: {leaked}")


class TestPhase6BRuntimeReconciliation(unittest.TestCase):
    """Test process-based task state reconciliation and startup recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.task_manager = TaskManager(root_tasks_dir=Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reconcile_active_task_remains_running(self):
        """Case A: Underlying worker/process is alive -> Keep RUNNING."""
        task = self.task_manager.create_task(
            title="Active Task",
            description="Processing",
            priority=TaskPriority.NORMAL,
        )
        self.task_manager.update_status(task.task_id, TaskStatus.RUNNING)

        # Pass task.task_id in active_task_ids
        stats = self.task_manager.reconcile_runtime_state(active_task_ids={task.task_id})
        self.assertEqual(stats["checked"], 1)
        self.assertEqual(stats["still_running"], 1)
        self.assertEqual(stats["completed"], 0)
        self.assertEqual(stats["failed"], 0)

        refreshed = self.task_manager.get_task(task.task_id)
        self.assertEqual(refreshed.status, TaskStatus.RUNNING)

    def test_reconcile_dead_process_with_success_becomes_completed(self):
        """Case B: Underlying worker died, but task result succeeded -> Mark COMPLETED."""
        task = self.task_manager.create_task(
            title="Succeeded Task",
            description="Done",
            priority=TaskPriority.NORMAL,
        )
        self.task_manager.update_status(task.task_id, TaskStatus.RUNNING)
        task = self.task_manager.get_task(task.task_id)
        task.result = {"success": True, "output": "Finished perfectly"}
        self.task_manager.save_task(task)

        # No active processes
        stats = self.task_manager.reconcile_runtime_state(active_task_ids=set())
        self.assertEqual(stats["completed"], 1)

        refreshed = self.task_manager.get_task(task.task_id)
        self.assertEqual(refreshed.status, TaskStatus.COMPLETED)
        self.assertEqual(refreshed.stage, "COMPLETE")
        self.assertTrue(refreshed.is_terminal)

    def test_reconcile_dead_process_with_error_becomes_failed(self):
        """Case C: Underlying worker died with errors recorded -> Mark FAILED."""
        task = self.task_manager.create_task(
            title="Failed Task",
            description="Crashed",
            priority=TaskPriority.NORMAL,
        )
        self.task_manager.update_status(task.task_id, TaskStatus.RUNNING)
        task = self.task_manager.get_task(task.task_id)
        task.errors.append("Subprocess exit code 1")
        self.task_manager.save_task(task)

        stats = self.task_manager.reconcile_runtime_state(active_task_ids=set())
        self.assertEqual(stats["failed"], 1)

        refreshed = self.task_manager.get_task(task.task_id)
        self.assertEqual(refreshed.status, TaskStatus.FAILED)
        self.assertEqual(refreshed.stage, "FAILED")

    def test_reconcile_dead_process_with_no_result_becomes_recovered(self):
        """Case D & E: Process dead and no result -> Mark FAILED/RECOVERED."""
        task = self.task_manager.create_task(
            title="Vanished Task",
            description="Killed by SIGKILL or machine restart",
            priority=TaskPriority.NORMAL,
        )
        self.task_manager.update_status(task.task_id, TaskStatus.RUNNING)

        stats = self.task_manager.reconcile_runtime_state(active_task_ids=set())
        self.assertEqual(stats["recovered"], 1)

        refreshed = self.task_manager.get_task(task.task_id)
        self.assertEqual(refreshed.status, TaskStatus.FAILED)
        self.assertEqual(refreshed.terminal_reason, "PROCESS_TERMINATED")
        self.assertTrue(any("recovered by runtime reconciler" in err for err in refreshed.errors))


class TestPhase6BTokenPersistence(unittest.TestCase):
    """Test durable token telemetry tracking and metrics endpoint format."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tracker = TokenTelemetryTracker(log_path=Path(self.temp_dir.name) / "tokens.jsonl")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_token_tracking_with_honest_unknowns(self):
        # 1. Reported tokens
        self.tracker.record_usage(
            task_id="t1",
            provider="cline",
            agent_id="cline",
            account_id="cli",
            requested_model="deepseek/deepseek-v4-flash",
            actual_model="deepseek/deepseek-v4-flash",
            input_tokens=1500,
            output_tokens=200,
            total_tokens=1700,
            usage_source="reported",
        )

        # 2. Unknown tokens (Kiro)
        self.tracker.record_usage(
            task_id="t2",
            provider="kiro",
            agent_id="kiro-cli",
            account_id="cli",
            requested_model="auto",
            actual_model="unknown",
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            usage_source="unknown",
        )

        # 3. Estimated tokens
        self.tracker.record_usage(
            task_id="t3",
            provider="antigravity",
            agent_id="antigravity-account-1",
            account_id="account-1",
            requested_model="gemini-2.5-pro",
            actual_model="gemini-2.5-pro",
            input_tokens=800,
            output_tokens=150,
            total_tokens=950,
            usage_source="estimated",
        )

        metrics = self.tracker.get_metrics()
        self.assertEqual(metrics["known_tokens"], 1700)
        self.assertEqual(metrics["estimated_tokens"], 950)
        self.assertEqual(metrics["unknown_usage_runs"], 1)

        self.assertIn("cline", metrics["by_provider"])
        self.assertIn("kiro", metrics["by_provider"])
        self.assertEqual(metrics["by_provider"]["kiro"]["unknown_usage_runs"], 1)
        self.assertEqual(metrics["by_provider"]["cline"]["known_tokens"], 1700)


class TestPhase6BSharedMemoryAndHandoffHistory(unittest.TestCase):
    """Test historical timeline for shared memory, BM25 retrieval events, and handoffs."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "memory.db"
        self.memory_store = MemoryStore(db_path=self.db_path)
        self.event_bus = EventBus(log_path=Path(self.temp_dir.name) / "events.jsonl")
        self.retriever = MemoryRetriever(self.memory_store, event_bus=self.event_bus)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_memory_retrieval_logging_and_events(self):
        """Every retrieval must record to SQLite and emit MEMORY_RETRIEVAL_COMPLETED event."""
        # Add memory
        self.memory_store.add(
            content="Authentication requires Bearer HMAC tokens",
            scope=MemoryScope.PROJECT,
            source_agent="antigravity-account-1",
            importance=5,
        )

        received_events = []
        self.event_bus.subscribe(
            lambda e: received_events.append(e) if e.event_type == EventType.MEMORY_RETRIEVAL_COMPLETED else None
        )

        # Retrieve
        results = self.retriever.retrieve_context(
            query="HMAC authentication tokens",
            task_id="task-audit-99",
            scope=MemoryScope.PROJECT,
        )
        self.assertGreaterEqual(len(results), 1)

        # Verify event was published
        self.assertEqual(len(received_events), 1)
        ev = received_events[0]
        self.assertEqual(ev.task_id, "task-audit-99")
        self.assertEqual(ev.payload["count"], 1)

        # Verify retrieval history persisted in SQLite
        history = self.memory_store.list_retrievals(task_id="task-audit-99")
        self.assertEqual(len(history), 1)
        record = history[0]
        self.assertEqual(record["task_id"], "task-audit-99")
        self.assertEqual(record["query"], "HMAC authentication tokens")
        self.assertIsNotNone(record["retrieved_memory_id"])
        self.assertGreater(record["retrieval_score"], 0)

    def test_handoff_history_archiving(self):
        """Handoffs are archived historically and can be retrieved by name."""
        base_dir = Path(self.temp_dir.name) / "handoffs"
        manager = HandoffManager(root_dir=base_dir)

        # Create two distinct handoffs
        rec1 = HandoffRecord(
            task="Inspect frontend",
            task_id="task-1",
            objective="Inspect frontend",
            agent_id="cline",
            recommended_agent="antigravity-account-1",
            next_action="Review styles",
        )
        manager.write_handoff(rec1)

        rec2 = HandoffRecord(
            task="Audit security",
            task_id="task-2",
            objective="Audit security",
            agent_id="antigravity-account-1",
            recommended_agent="kiro-cli",
            next_action="Run test suite",
        )
        manager.write_handoff(rec2)

        history = manager.list_history()
        self.assertGreaterEqual(len(history), 2)

        # Fetch individual record
        first_file = history[0]["filename"]
        record_detail = manager.get_record_by_name(first_file)
        self.assertIsNotNone(record_detail)
        self.assertIn(record_detail["task_id"], ("task-1", "task-2"))


if __name__ == "__main__":
    unittest.main()
