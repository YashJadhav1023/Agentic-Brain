"""Office state snapshot (ui/dashboard/office_state.py) and GET /api/office/state.

Every fixture lives in a temp dir: a fake BRAIN_DIR with a swarm pool, plus
in-memory fakes for Mission Control's managers. Nothing reads the live brain.
"""
from __future__ import annotations

import datetime
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from providers.registry.credential_manager import SecretRedactor
from ui.dashboard import office_state
from ui.dashboard.office_state import SnapshotCache, build_office_state, read_jsonl_tail

NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
FAKE_KEY = "sk-" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsT"  # matches the redactor's sk- pattern
XSS_TITLE = '<img src=x onerror="alert(1)"></script><script>alert(2)</script>'


def _ts(minutes_ago: float) -> str:
    return (NOW - datetime.timedelta(minutes=minutes_ago)).isoformat()


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def make_brain(root: Path, outside: Path) -> Path:
    brain = root / "agentic-brain"
    tasks = brain / "swarm" / "tasks"
    for state in ("pending", "in-progress", "completed", "escalated"):
        (tasks / state).mkdir(parents=True, exist_ok=True)
    _write(tasks / "pending" / "task-p1.json", {
        "id": "task-p1", "title": XSS_TITLE, "assigned_to": "cline", "confidence": 0.7,
        "rationale": "focused single-file work", "status": "pending", "created_at": _ts(1),
    })
    _write(tasks / "in-progress" / "task-r1.json", {
        "id": "task-r1", "title": "Run kubectl get pods", "assigned_to": "kiro-cli", "confidence": 1.7,
        "rationale": "terminal work", "status": "in-progress", "created_at": _ts(30),
        "started_at": _ts(29), "model": "claude-sonnet", "effort": "medium",
        "sandbox": {"branch": "brain/swarm/task-r1", "path": "/x", "repo": "/y", "repo_name": "y"},
    })
    _write(tasks / "in-progress" / "task-d1.json", {
        "id": "task-d1", "title": "Refactor the auth guards", "assigned_to": "antigravity-ide",
        "status": "in-progress", "created_at": _ts(20), "staged_at": _ts(19), "updated_at": _ts(19),
        "stage_state": "staged_for_antigravity-ide",
        "model_recommendation": {"tier": "frontier", "candidates": ["gemini-pro"], "requires_approval": True},
    })
    _write(tasks / "completed" / "task-c1.json", {
        "id": "task-c1", "title": "Design the schema", "assigned_to": "antigravity",
        "confidence": 0.9, "rationale": f"architecture work; used key {FAKE_KEY} by mistake",
        "status": "completed", "created_at": _ts(60), "started_at": _ts(59), "completed_at": _ts(40),
        "duration_seconds": 1140, "model": "gemini-3.7-flash-medium",
        "antigravity_account": "antigravity-api",
        "antigravity_attempts": [
            "antigravity: out of capacity on gemini-3.8-flash-medium",
            "antigravity-api: ran on gemini-3.7-flash-medium",
        ],
        "sandbox_result": {"branch": "brain/swarm/task-c1", "changed": True},
        "output": "FULL OUTPUT BODY that must never be served " * 50,
    })
    _write(tasks / "escalated" / "task-e1.json", {
        "id": "task-e1", "title": "Fix the flaky test", "assigned_to": "cline", "status": "escalated",
        "created_at": _ts(8), "started_at": _ts(7), "completed_at": _ts(2),
        "error": f"Traceback with {FAKE_KEY} " + "E" * 5000,
    })
    # Malformed JSON and a non-object JSON document: skipped, never fatal.
    _write(tasks / "completed" / "broken.json", "{not json")
    _write(tasks / "completed" / "list.json", "[1, 2, 3]")
    # A symlink that escapes BRAIN_DIR: its target is a perfectly valid task.
    _write(outside / "escape.json", {"id": "task-escape", "title": "outside", "status": "completed",
                                     "created_at": _ts(1)})
    os.symlink(outside / "escape.json", tasks / "completed" / "escape.json")
    # A FIFO would block a naive read forever.
    if hasattr(os, "mkfifo"):
        os.mkfifo(tasks / "completed" / "pipe.json")
    _write(brain / "handoff" / "current.md", (
        "---\ntitle: current\ntype: handoff\n---\n\n"
        "# Wire the Office view\n\n## Observations\n"
        "- [status] in-progress\n- [agent] kiro-cli\n- [scope] repo\n"
        f"- [next] rotate {FAKE_KEY} then run the suite\n- [blocker] none\n"
    ))
    return brain


class FakeMemory:
    def count(self):
        return 42

    def list_all(self, limit=100):
        return [SimpleNamespace(created_at=_ts(3), source_agent="antigravity-account-1",
                                scope=SimpleNamespace(value="PROJECT"), task_id="mc-1",
                                content=f"secret memory body {FAKE_KEY}")][:limit]


class FakeHandoffs:
    def get_current_record(self):
        return SimpleNamespace(task="MC handoff", created_at=_ts(4), task_id="mc-1",
                               agent_id="antigravity-account-1", recommended_agent="kiro-cli",
                               next_action="continue the refactor", is_terminal=False)


def mc_task(tid, status, agent, minutes_ago, **kw):
    return SimpleNamespace(
        task_id=tid, title=kw.get("title", f"MC {tid}"), status=SimpleNamespace(value=status),
        assigned_agent=agent, assigned_account=agent, assigned_model="auto", actual_model=kw.get("model"),
        complexity="standard", requires_approval=kw.get("requires_approval", False),
        created_at=_ts(minutes_ago), started_at=kw.get("started_at"), completed_at=kw.get("completed_at"),
        duration_seconds=0.0, errors=kw.get("errors", []),
    )


def account(aid, provider, status="ONLINE", enabled=True, account_type="agent"):
    return SimpleNamespace(id=aid, provider_id=provider, display_name=aid.title(), enabled=enabled,
                           status=SimpleNamespace(value=status), account_type=account_type)


class OfficeStateFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.brain = make_brain(self.tmp, self.outside)
        self.redactor = SecretRedactor()

    def build(self, **kw):
        args = dict(
            mc_tasks=[
                mc_task("mc-1", "RUNNING", "antigravity-account-1", 5, started_at=_ts(4), model="gemini-x"),
                mc_task("mc-2", "PLANNING", None, 6),
                mc_task("mc-3", "FAILED", "cline-account-1", 50, completed_at=_ts(45),
                        errors=[f"boom {FAKE_KEY}"]),
                mc_task("task-c1", "COMPLETED", "kiro-cli", 70),  # duplicate id: swarm record wins
            ],
            jobs=[SimpleNamespace(
                id="job-1", task="API job", status="failed", worker=None, account="openai-generic-1",
                provider="openai", model="gpt-x", created_at=_ts(15), started_at=_ts(14),
                completed_at=_ts(13), duration=None, error=f"401 from upstream {FAKE_KEY}",
                metadata={"routing_decision": {"reason": "cheapest api", "complexity": "SIMPLE"},
                          "failover_history": [{"original_account": "openai-generic-1",
                                                "reason": "Execution failed; triggering failover"}]},
            )],
            routing_history=[
                {"timestamp": _ts(14), "job_id": "job-1", "selected_account": "openai-generic-1",
                 "reason": "duplicate of job-1's own route flow"},
                {"timestamp": _ts(9), "job_id": "job-gone", "selected_account": "openai-generic-1",
                 "reason": f"score 0.9 {FAKE_KEY}"},
            ],
            memory_store=FakeMemory(),
            handoff_manager=FakeHandoffs(),
            accounts=[
                account("antigravity-account-1", "antigravity"),
                account("kiro-cli", "kiro"),
                account("cline-account-1", "cline", status="RATE_LIMITED"),
                account("cline-account-2", "cline", enabled=False),
                account("openai-generic-1", "openai", account_type="api"),
            ],
            brain_root=self.brain,
            redactor=self.redactor,
            now=NOW,
        )
        args.update(kw)
        return build_office_state(**args)


class TestOfficeStateSchema(OfficeStateFixture):
    def test_top_level_contract(self):
        state = self.build()
        for key in ("generated_at", "sources", "agents", "tasks", "flows", "memory", "handoff"):
            self.assertIn(key, state)
        self.assertEqual(state["sources"]["mission_control"], True)
        self.assertIs(state["sources"]["brain_swarm"], True)
        self.assertEqual(state["sources"]["brain_dir"], str(self.brain.resolve()))
        task_keys = {"id", "title", "stage", "agent", "kind", "model", "model_tier", "complexity", "risk",
                     "routing_reason", "confidence", "requires_approval", "created_at", "started_at",
                     "completed_at", "duration_s", "sandbox_branch", "attempts", "source"}
        for t in state["tasks"]:
            self.assertTrue(task_keys <= set(t), t)
            self.assertNotIn("_error", t)
            self.assertIn(t["stage"], ("queued", "routing", "running", "review", "done", "escalated", "delivered"))
            self.assertIn(t["source"], ("swarm", "mission-control"))
            if t["confidence"] is not None:
                self.assertTrue(0 <= t["confidence"] <= 1)
        agent_keys = {"id", "label", "kind", "account", "status", "current_task_id", "model", "source"}
        for a in state["agents"]:
            self.assertTrue(agent_keys <= set(a), a)
            self.assertIn(a["status"], ("idle", "working", "blocked", "offline"))
            self.assertIn(a["kind"], office_state.AGENT_KINDS)
        flow_types = {"dispatch", "route", "start", "finish", "escalate", "failover", "memory_write", "handoff", "deliver"}
        for f in state["flows"]:
            self.assertEqual(set(f), {"ts", "type", "task_id", "from", "to", "detail"})
            self.assertIn(f["type"], flow_types)
            self.assertLessEqual(len(f["detail"]), office_state.DETAIL_CHARS)
        json.dumps(state)  # serialisable as-is

    def test_swarm_stages_and_fields(self):
        tasks = {t["id"]: t for t in self.build()["tasks"]}
        self.assertEqual(tasks["task-p1"]["stage"], "queued")
        self.assertEqual(tasks["task-r1"]["stage"], "running")
        self.assertEqual(tasks["task-r1"]["confidence"], 1.0)  # clamped
        self.assertEqual(tasks["task-r1"]["sandbox_branch"], "brain/swarm/task-r1")
        self.assertEqual(tasks["task-d1"]["stage"], "delivered")
        self.assertEqual(tasks["task-d1"]["kind"], "antigravity-ide")
        self.assertEqual(tasks["task-d1"]["model_tier"], "frontier")
        self.assertIsNone(tasks["task-d1"]["model"], "a recommendation is never reported as the model used")
        self.assertTrue(tasks["task-d1"]["requires_approval"])
        self.assertEqual(tasks["task-c1"]["stage"], "done")
        self.assertEqual(tasks["task-c1"]["source"], "swarm")
        self.assertEqual(tasks["task-c1"]["duration_s"], 1140)
        self.assertEqual(tasks["task-c1"]["sandbox_branch"], "brain/swarm/task-c1")
        self.assertEqual(len(tasks["task-c1"]["attempts"]), 2)
        self.assertEqual(tasks["task-e1"]["stage"], "escalated")
        self.assertEqual(tasks["mc-2"]["stage"], "routing")
        self.assertEqual(tasks["mc-3"]["stage"], "escalated")
        self.assertEqual(tasks["job-1"]["stage"], "escalated")
        self.assertEqual(tasks["job-1"]["routing_reason"], "cheapest api")

    def test_most_recent_first(self):
        tasks = self.build()["tasks"]
        latest = [office_state._latest_ts(t) for t in tasks]
        self.assertEqual(latest, sorted(latest, reverse=True))
        flows = [f["ts"] for f in self.build()["flows"]]
        self.assertEqual(flows, sorted(flows, key=lambda s: office_state.parse_ts(s), reverse=True))


class TestOfficeStateSafety(OfficeStateFixture):
    def test_malformed_symlink_and_fifo_are_skipped(self):
        state = self.build()
        ids = {t["id"] for t in state["tasks"]}
        self.assertNotIn("task-escape", ids, "symlink outside BRAIN_DIR must not be followed")
        self.assertNotIn("broken", ids)
        self.assertNotIn("list", ids)
        self.assertNotIn("pipe", ids)
        self.assertGreaterEqual(state["sources"]["skipped_files"], 3)

    def test_symlinked_state_folder_outside_brain_is_ignored(self):
        shutil.rmtree(self.brain / "swarm" / "tasks" / "escalated")
        foreign = self.outside / "escalated"
        _write(foreign / "task-x.json", {"id": "task-x", "title": "foreign", "created_at": _ts(1)})
        os.symlink(foreign, self.brain / "swarm" / "tasks" / "escalated")
        ids = {t["id"] for t in self.build()["tasks"]}
        self.assertNotIn("task-x", ids)

    def test_oversized_file_is_skipped(self):
        with mock.patch.object(office_state, "MAX_FILE_BYTES", 600):
            ids = {t["id"] for t in self.build(mc_tasks=[])["tasks"]}
        self.assertNotIn("task-c1", ids)  # its embedded output makes it large
        self.assertIn("task-p1", ids)

    def test_secrets_redacted_everywhere(self):
        blob = json.dumps(self.build())
        self.assertNotIn(FAKE_KEY, blob)
        self.assertNotIn(FAKE_KEY[:12], blob, "truncation must not leak a secret prefix")
        self.assertIn(SecretRedactor.REDACTION_TOKEN, blob)

    def test_output_bodies_and_long_errors_never_served(self):
        state = self.build()
        blob = json.dumps(state)
        self.assertNotIn("FULL OUTPUT BODY", blob)
        self.assertNotIn("secret memory body", blob)
        self.assertNotIn("E" * 200, blob)
        esc = [f for f in state["flows"] if f["type"] == "escalate" and f["task_id"] == "task-e1"]
        self.assertEqual(len(esc), 1)
        self.assertLessEqual(len(esc[0]["detail"]), office_state.DETAIL_CHARS)
        self.assertIn(SecretRedactor.REDACTION_TOKEN, esc[0]["detail"])

    def test_xss_title_is_inert_data_and_truncated(self):
        t = next(t for t in self.build()["tasks"] if t["id"] == "task-p1")
        # Delivered verbatim as a JSON string; the client escapes it.
        self.assertEqual(t["title"], XSS_TITLE[: office_state.TITLE_CHARS])
        long = self.build(mc_tasks=[mc_task("mc-long", "READY", None, 1, title="x" * 1000)])
        t = next(t for t in long["tasks"] if t["id"] == "mc-long")
        self.assertLessEqual(len(t["title"]), office_state.TITLE_CHARS)

    def test_caps(self):
        many = [mc_task(f"mc-{i}", "READY", None, i) for i in range(200)]
        state = self.build(mc_tasks=many)
        self.assertLessEqual(len(state["tasks"]), office_state.MAX_TASKS)
        self.assertLessEqual(len(state["flows"]), office_state.MAX_FLOWS)

    def test_brain_dir_missing(self):
        state = self.build(brain_root=self.tmp / "does-not-exist")
        self.assertIs(state["sources"]["brain_swarm"], False)
        self.assertIsNone(state["sources"]["brain_dir"])
        self.assertTrue(all(t["source"] == "mission-control" for t in state["tasks"]))
        self.assertIn("mc-1", {t["id"] for t in state["tasks"]})
        self.assertEqual(state["handoff"]["title"], "MC handoff")  # falls back to Mission Control's
        self.assertEqual(state["handoff"]["source"], "mission-control")
        state = self.build(brain_root=None)
        self.assertIs(state["sources"]["brain_swarm"], False)

    def test_failing_sources_fail_soft(self):
        class Boom:
            def count(self):
                raise RuntimeError("db locked")

            def list_all(self, limit=100):
                raise RuntimeError("db locked")

            def get_current_record(self):
                raise RuntimeError("bad json")

        state = self.build(memory_store=Boom(), handoff_manager=Boom())
        self.assertEqual(state["memory"], {"entries": 0, "recent": []})
        self.assertEqual(state["handoff"]["title"], "Wire the Office view")


class TestOfficeStateFlowsAndAgents(OfficeStateFixture):
    def flows(self, **kw):
        return self.build(**kw)["flows"]

    def test_task_lifecycle_flows(self):
        flows = self.flows()
        by = lambda tid, typ: [f for f in flows if f["task_id"] == tid and f["type"] == typ]
        self.assertEqual(len(by("task-c1", "dispatch")), 1)
        self.assertEqual(by("task-c1", "route")[0]["to"], "antigravity")
        self.assertEqual(len(by("task-c1", "start")), 1)
        self.assertEqual(by("task-c1", "finish")[0]["from"], "antigravity")
        fo = by("task-c1", "failover")
        self.assertEqual(len(fo), 1)
        self.assertEqual((fo[0]["from"], fo[0]["to"]), ("antigravity", "antigravity-api"))
        # Delivery is not execution: a deliver flow, and no start flow.
        self.assertEqual(by("task-d1", "deliver")[0]["to"], "antigravity-ide")
        self.assertEqual(by("task-d1", "start"), [])
        self.assertEqual(len(by("job-1", "failover")), 0)  # a single attempt has no hand-off target
        self.assertEqual(len(by("job-1", "escalate")), 1)

    def test_manager_flows(self):
        types = {f["type"] for f in self.flows()}
        self.assertTrue({"memory_write", "handoff", "route"} <= types)
        routes = [f for f in self.flows() if f["type"] == "route" and f["from"] == "router"]
        self.assertEqual([f["task_id"] for f in routes], ["job-gone"])  # job-1 is not animated twice
        mem = [f for f in self.flows() if f["type"] == "memory_write"][0]
        self.assertEqual((mem["from"], mem["to"]), ("antigravity-account-1", "memory"))

    def test_memory_and_handoff(self):
        state = self.build()
        self.assertEqual(state["memory"]["entries"], 42)
        self.assertEqual(state["memory"]["recent"][0],
                         {"ts": _ts(3), "agent": "antigravity-account-1", "scope": "PROJECT"})
        h = state["handoff"]
        self.assertEqual(h["title"], "Wire the Office view")
        self.assertEqual(h["status"], "in-progress")
        self.assertEqual(h["agent"], "kiro-cli")
        self.assertIn(SecretRedactor.REDACTION_TOKEN, h["next"])
        self.assertIsNotNone(h["updated_at"])

    def test_agent_status(self):
        agents = {a["id"]: a for a in self.build()["agents"]}
        self.assertEqual(agents["kiro-cli"]["status"], "working")  # registry account + swarm task-r1
        self.assertEqual(agents["kiro-cli"]["current_task_id"], "task-r1")
        self.assertEqual(agents["kiro-cli"]["source"], "mission-control")
        self.assertEqual(agents["antigravity-account-1"]["status"], "working")
        self.assertEqual(agents["antigravity-account-1"]["model"], "gemini-x")
        # task-e1 escalated 2 min ago; a newer *queued* task does not unblock it.
        self.assertEqual(agents["cline"]["status"], "blocked")
        self.assertEqual(agents["cline"]["source"], "swarm")
        self.assertEqual(agents["cline-account-1"]["status"], "blocked")  # RATE_LIMITED
        self.assertEqual(agents["cline-account-2"]["status"], "offline")  # disabled
        self.assertEqual(agents["openai-generic-1"]["kind"], "api")
        ide = agents["antigravity-ide"]
        self.assertEqual((ide["status"], ide["current_task_id"]), ("idle", "task-d1"))
        self.assertEqual(agents["antigravity"]["status"], "idle")
        self.assertEqual(agents["antigravity-api"]["kind"], "antigravity-api")

    def test_old_escalation_does_not_block(self):
        later = NOW + datetime.timedelta(minutes=30)
        agents = {a["id"]: a for a in self.build(now=later)["agents"]}
        self.assertEqual(agents["cline"]["status"], "idle")


class TestHelpers(unittest.TestCase):
    def test_read_jsonl_tail_is_bounded_and_tolerant(self):
        tmp = Path(tempfile.mkdtemp(prefix="office-jsonl-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "h.jsonl"
        lines = [json.dumps({"n": i}) for i in range(1000)] + ["{broken", "[1]", ""]
        p.write_text("\n".join(lines), encoding="utf-8")
        out = read_jsonl_tail(p, limit=5, max_bytes=2048)
        self.assertEqual([r["n"] for r in out], [999, 998, 997, 996, 995])
        self.assertEqual(read_jsonl_tail(tmp / "missing.jsonl"), [])
        self.assertEqual(read_jsonl_tail(None), [])

    def test_snapshot_cache(self):
        calls = []
        cache = SnapshotCache(ttl=60)
        build = lambda: calls.append(1) or {"n": len(calls)}
        self.assertEqual(cache.get("a", build), {"n": 1})
        self.assertEqual(cache.get("a", build), {"n": 1})
        self.assertEqual(cache.get("b", build), {"n": 2})  # key change rebuilds
        cache.clear()
        self.assertEqual(cache.get("b", build), {"n": 3})

    def test_clean_redacts_before_truncating(self):
        r = SecretRedactor()
        out = office_state.clean("x" * 10 + " " + FAKE_KEY, r, 14)
        self.assertLessEqual(len(out), 14)
        self.assertNotIn("sk-", out)
        self.assertTrue(out.startswith("x" * 10 + " *"))
        self.assertIsNone(office_state.clean(None, r))


class TestOfficeRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ui.dashboard import dashboard

        cls.dashboard = dashboard
        cls.server = dashboard.ThreadedHTTPServer(("127.0.0.1", 0), dashboard.MissionControlHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = dashboard.get_or_create_auth_token()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-route-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dashboard._office_cache.clear()
        self.addCleanup(self.dashboard._office_cache.clear)

    def get(self, auth=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
            conn.request("GET", "/api/office/state", headers=headers)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read().decode("utf-8")
        finally:
            conn.close()

    def test_not_public(self):
        self.assertFalse(self.dashboard.is_public_path("/api/office/state"))

    def test_401_without_or_with_wrong_token(self):
        status, _, _ = self.get(auth=False)
        self.assertEqual(status, 401)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("GET", "/api/office/state", headers={"Authorization": "Bearer wrong"})
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()

    def test_200_schema_with_swarm_fixture(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        brain = make_brain(self.tmp, outside)
        with mock.patch.dict(os.environ, {"BRAIN_DIR": str(brain)}):
            status, ctype, body = self.get()
        self.assertEqual(status, 200, body)
        self.assertTrue(ctype.startswith("application/json"))
        state = json.loads(body)
        for key in ("generated_at", "sources", "agents", "tasks", "flows", "memory", "handoff"):
            self.assertIn(key, state)
        self.assertIs(state["sources"]["brain_swarm"], True)
        self.assertIs(state["sources"]["mission_control"], True)
        ids = {t["id"] for t in state["tasks"]}
        self.assertTrue({"task-p1", "task-r1", "task-d1", "task-c1", "task-e1"} <= ids)
        self.assertNotIn("task-escape", ids)
        self.assertNotIn(FAKE_KEY, body)
        self.assertNotIn("FULL OUTPUT BODY", body)
        self.assertIsInstance(state["tasks"][0]["requires_approval"], bool)
        self.assertIsInstance(state["memory"]["entries"], int)

    def test_brain_dir_missing(self):
        with mock.patch.dict(os.environ, {"BRAIN_DIR": str(self.tmp / "nope")}):
            status, _, body = self.get()
        self.assertEqual(status, 200, body)
        state = json.loads(body)
        self.assertIs(state["sources"]["brain_swarm"], False)
        self.assertIsNone(state["sources"]["brain_dir"])
        self.assertIsInstance(state["agents"], list)
        self.assertTrue(state["agents"], "registry agents are still reported without the brain")


if __name__ == "__main__":
    unittest.main()
