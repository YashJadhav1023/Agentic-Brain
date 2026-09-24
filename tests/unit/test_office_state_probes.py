"""Adversarial probes (verifier round 1) for GET /api/office/state.

Run from .verify1/src with HOME/BRAIN_DIR pointing into .verify1.
Each test asserts the *desired* behaviour; a failure is a finding.
"""
from __future__ import annotations

import datetime
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from providers.registry.credential_manager import SecretRedactor
from ui.dashboard import office_state
from ui.dashboard.office_state import build_office_state

NOW = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.timezone.utc)
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"   # 40 chars, matches gh pattern
SK = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
AIZA = "AIza" + "B" * 35


def ts(minutes_ago):
    return (NOW - datetime.timedelta(minutes=minutes_ago)).isoformat()


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="office-probe-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.brain = self.tmp / "brain"
        for s in ("pending", "in-progress", "completed", "escalated"):
            (self.brain / "swarm" / "tasks" / s).mkdir(parents=True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        self.red = SecretRedactor()
        write(self.brain / "swarm/tasks/completed/ok.json",
              {"id": "task-ok", "title": "fine", "assigned_to": "cline", "created_at": ts(3),
               "started_at": ts(2), "completed_at": ts(1)})

    def build(self, **kw):
        args = dict(brain_root=self.brain, redactor=self.red, now=NOW)
        args.update(kw)
        return build_office_state(**args)


class P1_DeepNestingJSON(Base):
    """A swarm file nested past the recursion limit must be skipped, not 500 the endpoint."""

    def test_deeply_nested_json_is_skipped(self):
        write(self.brain / "swarm/tasks/pending/deep.json", "[" * 200000 + "]" * 200000)
        state = self.build()  # must not raise
        ids = {t["id"] for t in state["tasks"]}
        self.assertIn("task-ok", ids)
        self.assertGreaterEqual(state["sources"]["skipped_files"], 1)

    def test_deeply_nested_value_inside_valid_record(self):
        write(self.brain / "swarm/tasks/pending/deepval.json",
              '{"id":"task-deepval","title":' + "[" * 200000 + "]" * 200000 + "}")
        state = self.build()
        self.assertIn("task-ok", {t["id"] for t in state["tasks"]})


class P2_SecretAtRedactionWindow(Base):
    """Whitespace padding must not let a secret prefix survive the 4096-char pre-cut."""

    def test_whitespace_padded_secret_prefix_not_leaked(self):
        # 4000 spaces collapse to one, but the pre-collapse cut at 4096 lands inside the key.
        title = "deploy" + " " * 4058 + GH  # cut leaves ghp_ + 28 of 36 chars
        write(self.brain / "swarm/tasks/pending/pad.json",
              {"id": "task-pad", "title": title, "assigned_to": "kiro-cli", "created_at": ts(1)})
        state = self.build()
        body = json.dumps(state)
        t = next(t for t in state["tasks"] if t["id"] == "task-pad")
        # The first 8+ chars of the token after the ghp_ prefix must not appear.
        self.assertNotIn(GH[:20], body, f"secret prefix leaked in title: {t['title']!r}")


class P3_SecretsInEveryField(Base):
    def test_every_string_field_is_redacted(self):
        write(self.brain / "swarm/tasks/escalated/sec.json", {
            "id": "task-sec", "title": f"use {SK}", "assigned_to": "antigravity",
            "rationale": f"Bearer {'x' * 30}", "model": f"m {AIZA}", "model_rationale": GH,
            "antigravity_account": f"acct {SK}", "antigravity_attempts": [f"antigravity: exit 1 {GH}", f"antigravity-api: {SK}"],
            "sandbox": {"branch": f"brain/swarm/{GH}"}, "error": f"Traceback api_key={SK}",
            "output": f"FULL OUTPUT {SK}", "created_at": ts(5), "started_at": ts(4), "completed_at": ts(3),
            "complexity": SK, "risk": GH, "effort": AIZA,
        })
        write(self.brain / "handoff/current.md",
              f"---\ntitle: current\n---\n# Rotate {SK}\n\n- [status] in-progress\n- [agent] {GH}\n- [next] curl -H 'Authorization: Bearer {'y' * 40}'\n")
        state = self.build(routing_history=[{"timestamp": ts(2), "job_id": "j", "selected_account": SK, "reason": GH}])
        body = json.dumps(state)
        for secret in (SK, GH, AIZA, "x" * 30, "y" * 40, SK[:16], GH[:16]):
            self.assertNotIn(secret, body, f"secret fragment {secret[:10]}... leaked")
        self.assertNotIn("FULL OUTPUT", body)
        # a short redacted error summary in the escalate detail is allowed by the contract


class P4_SymlinkAndTraversal(Base):
    def test_symlinked_handoff_outside_is_refused(self):
        write(self.outside / "secret.md", "# OUTSIDE TITLE\n- [status] done\n- [agent] evil\n- [next] exfil\n")
        (self.brain / "handoff").mkdir()
        (self.brain / "handoff" / "current.md").symlink_to(self.outside / "secret.md")
        state = self.build()
        self.assertNotIn("OUTSIDE TITLE", json.dumps(state))

    def test_symlinked_handoff_dir_outside_is_refused(self):
        write(self.outside / "h/current.md", "# OUTSIDE DIR TITLE\n- [status] done\n")
        (self.brain / "handoff").symlink_to(self.outside / "h")
        state = self.build()
        self.assertNotIn("OUTSIDE DIR TITLE", json.dumps(state))

    def test_symlinked_swarm_root_outside_is_refused(self):
        shutil.rmtree(self.brain / "swarm")
        write(self.outside / "sw/tasks/pending/x.json", {"id": "task-outside-root", "title": "OUTSIDE"})
        (self.brain / "swarm").symlink_to(self.outside / "sw")
        state = self.build()
        self.assertNotIn("task-outside-root", json.dumps(state))
        self.assertFalse(state["sources"]["brain_swarm"])

    def test_relative_dotdot_symlink_escape(self):
        write(self.outside / "esc.json", {"id": "task-dotdot", "title": "ESCAPED"})
        (self.brain / "swarm/tasks/pending/esc.json").symlink_to(Path("../../../../outside/esc.json"))
        state = self.build()
        self.assertNotIn("task-dotdot", json.dumps(state))

    def test_brain_dir_itself_a_symlink_still_works(self):
        link = self.tmp / "brain-link"
        link.symlink_to(self.brain)
        state = self.build(brain_root=link)
        self.assertTrue(state["sources"]["brain_swarm"])
        self.assertIn("task-ok", {t["id"] for t in state["tasks"]})

    def test_symlink_loop_does_not_hang(self):
        (self.brain / "swarm/tasks/pending/loop.json").symlink_to(self.brain / "swarm/tasks/pending/loop.json")
        state = self.build()
        self.assertIn("task-ok", {t["id"] for t in state["tasks"]})

    def test_brain_dir_is_a_file(self):
        f = self.tmp / "file"
        f.write_text("x")
        state = self.build(brain_root=f)
        self.assertFalse(state["sources"]["brain_swarm"])
        self.assertIsNone(state["sources"]["brain_dir"])


class P5_HugeAndMany(Base):
    def test_many_files_bounded_time_and_count(self):
        pend = self.brain / "swarm/tasks/pending"
        for i in range(3000):
            (pend / f"t{i}.json").write_text(json.dumps({"id": f"task-{i}", "title": "x", "created_at": ts(i % 50)}))
        t0 = time.monotonic()
        state = self.build()
        dt = time.monotonic() - t0
        self.assertLessEqual(len(state["tasks"]), 60)
        self.assertLessEqual(len(state["flows"]), 100)
        self.assertLess(dt, 3.0, f"snapshot took {dt:.2f}s with 3000 files")

    def test_sparse_huge_file_not_read(self):
        p = self.brain / "swarm/tasks/pending/huge.json"
        with open(p, "wb") as fh:
            fh.truncate(3 * 1024 ** 3)  # 3 GiB sparse
        t0 = time.monotonic()
        state = self.build()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertGreaterEqual(state["sources"]["skipped_files"], 1)

    def test_huge_handoff_ignored(self):
        write(self.brain / "handoff/current.md", "# T\n" + "- [next] a\n" * 200000)
        state = self.build()
        self.assertIsNone(state["handoff"]["title"])

    def test_huge_int_and_nan_fields(self):
        write(self.brain / "swarm/tasks/pending/num.json",
              '{"id":"task-num","title":"n","confidence":' + "9" * 5000 + ',"duration_seconds":NaN,"created_at":"' + ts(1) + '"}')
        write(self.brain / "swarm/tasks/pending/inf.json",
              '{"id":"task-inf","title":"n","confidence":1e999,"duration_seconds":-Infinity,"created_at":"' + ts(1) + '"}')
        state = self.build()
        body = json.dumps(state, allow_nan=False)  # response must be strict JSON
        self.assertIn("task-inf", body)


class P6_WeirdRecords(Base):
    def test_bad_types_and_timestamps(self):
        recs = {
            "a": [1, 2, 3],
            "b": {"id": 123, "title": "int id"},
            "c": {"id": "task-c", "title": {"nested": ["x"]}, "created_at": "0001-01-01T00:00:00+01:00",
                  "antigravity_attempts": "not-a-list", "sandbox": "str", "model_recommendation": ["x"]},
            "d": {"id": "task-d", "title": None, "created_at": "9999-12-31T23:59:59-01:00", "started_at": 12,
                  "completed_at": ["x"], "confidence": "high", "assigned_to": ["kiro-cli"]},
            "e": {"id": "task-e", "title": "ok", "created_at": "garbage", "assigned_to": "<img src=x onerror=alert(1)>"},
        }
        for n, r in recs.items():
            write(self.brain / f"swarm/tasks/pending/{n}.json", r)
        state = self.build()
        ids = {t["id"] for t in state["tasks"]}
        self.assertIn("task-ok", ids)
        self.assertIn("task-e", ids)
        json.dumps(state, allow_nan=False)
        for a in state["agents"]:
            self.assertIn(a["kind"], office_state.AGENT_KINDS)
            self.assertIn(a["status"], ("idle", "working", "blocked", "offline"))

    def test_overflow_timestamp_in_routing_history_does_not_500(self):
        state = self.build(routing_history=[{"timestamp": "0001-01-01T00:00:00+05:00", "job_id": "j9",
                                             "selected_account": "x", "reason": "r"}])
        self.assertIn("flows", state)

    def test_overflow_timestamp_in_memory_does_not_500(self):
        class M:
            def count(self):
                return 1

            def list_all(self, limit=10):
                from types import SimpleNamespace
                return [SimpleNamespace(scope="project", created_at="0001-01-01T00:00:00+05:00", source_agent="a", task_id=None)]

        state = self.build(memory_store=M())
        self.assertEqual(state["memory"]["entries"], 1)


class P7_DeliverySemantics(Base):
    """Acknowledgement is receipt, not execution (AGENTS.md)."""

    def _ide(self, stage_state, **extra):
        rec = {"id": "task-ide", "title": "IDE work", "assigned_to": "antigravity-ide", "status": "in-progress",
               "stage_state": stage_state, "created_at": ts(5), "staged_at": ts(4), "updated_at": ts(1),
               "model_recommendation": {"tier": "deep", "candidates": ["gemini-3.8-flash-medium"]}}
        rec.update(extra)
        write(self.brain / "swarm/tasks/in-progress/ide.json", rec)
        return self.build()

    def test_staged_is_delivered_with_no_model_or_start(self):
        st = self._ide("staged_for_antigravity-ide")
        t = next(t for t in st["tasks"] if t["id"] == "task-ide")
        self.assertEqual(t["stage"], "delivered")
        self.assertIsNone(t["model"])
        self.assertIsNone(t["started_at"])
        self.assertFalse([f for f in st["flows"] if f["task_id"] == "task-ide" and f["type"] == "start"])
        ide = next(a for a in st["agents"] if a["id"] == "antigravity-ide")
        self.assertNotEqual(ide["status"], "working")

    def test_acknowledged_delivery_is_not_shown_as_running(self):
        st = self._ide("acknowledged_by_antigravity-ide", model_reported="gemini-3.8-flash-medium")
        t = next(t for t in st["tasks"] if t["id"] == "task-ide")
        ide = next(a for a in st["agents"] if a["id"] == "antigravity-ide")
        self.assertNotEqual(t["stage"], "running", "acknowledgement (receipt) rendered as execution")
        self.assertNotEqual(ide["status"], "working", "acknowledgement (receipt) rendered as working")


class P8_RouteAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from ui.dashboard import dashboard
        cls.d = dashboard
        cls.server = dashboard.ThreadedHTTPServer(("127.0.0.1", 0), dashboard.MissionControlHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.token = dashboard.get_or_create_auth_token()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            c.close()

    def test_query_token_not_accepted(self):
        s, _ = self.req("GET", f"/api/office/state?token={self.token}")
        self.assertEqual(s, 401)
        s, _ = self.req("GET", f"/api/office/state?access_token={self.token}")
        self.assertEqual(s, 401)

    def test_other_schemes_and_path_tricks(self):
        for hdr in ({"Authorization": f"Basic {self.token}"}, {"Authorization": f"bearer{self.token}"},
                    {"Authorization": "Bearer "}, {"Cookie": f"token={self.token}"}):
            s, _ = self.req("GET", "/api/office/state", hdr)
            self.assertEqual(s, 401, hdr)
        for p in ("/api/office/state/", "/api/office/state?", "/api/office/state#x", "/api//office/state",
                  "/static/../api/office/state", "/api/office/state%00"):
            s, body = self.req("GET", p)
            self.assertIn(s, (401, 403, 404), f"{p} -> {s}")
            self.assertNotIn('"agents"', body, p)

    def test_cross_origin_rejected(self):
        s, body = self.req("GET", "/api/office/state", {"Authorization": f"Bearer {self.token}",
                                                         "Origin": "http://evil.example"})
        self.assertNotEqual(s, 200)
        self.assertNotIn('"agents"', body)

    def test_poisoned_brain_does_not_500_or_leak(self):
        tmp = Path(tempfile.mkdtemp(prefix="office-probe-route-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for s in ("pending", "escalated"):
            (tmp / "swarm/tasks" / s).mkdir(parents=True)
        write(tmp / "swarm/tasks/pending/deep.json", "[" * 200000)
        write(tmp / "swarm/tasks/escalated/s.json", {"id": "task-s", "title": f"t {SK}", "error": GH,
                                                     "created_at": ts(1), "completed_at": ts(0)})
        self.d._office_cache.clear()
        with mock.patch.dict(os.environ, {"BRAIN_DIR": str(tmp)}):
            s, body = self.req("GET", "/api/office/state", {"Authorization": f"Bearer {self.token}"})
        self.d._office_cache.clear()
        self.assertEqual(s, 200, body[:300])
        self.assertNotIn(SK, body)
        self.assertNotIn(GH, body)

    def test_cache_hit_is_fast_under_parallel_poll(self):
        self.d._office_cache.clear()
        results = []

        def hit():
            results.append(self.req("GET", "/api/office/state", {"Authorization": f"Bearer {self.token}"})[0])

        threads = [threading.Thread(target=hit) for _ in range(12)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 12)
        self.assertLess(time.monotonic() - t0, 20)


if __name__ == "__main__":
    unittest.main()
