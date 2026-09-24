"""Round-2 adversarial probes for GET /api/office/state (office-api).

Run from a source export with HOME/BRAIN_DIR/BASIC_MEMORY_CONFIG_DIR pointed at
scratch dirs. None of these were in the implementer's or verifier-1's suites.
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

NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
KEY = "sk-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1"


def _ts(m: float) -> str:
    return (NOW - datetime.timedelta(minutes=m)).isoformat()


def make_brain(root: Path) -> Path:
    brain = root / "brain"
    for s in ("pending", "in-progress", "completed", "escalated"):
        (brain / "swarm" / "tasks" / s).mkdir(parents=True, exist_ok=True)
    (brain / "handoff").mkdir(parents=True, exist_ok=True)
    (brain / "swarm" / "tasks" / "completed" / "task-ok.json").write_text(json.dumps({
        "id": "task-ok", "title": "fine", "assigned_to": "kiro-cli", "status": "completed",
        "created_at": _ts(5), "started_at": _ts(4), "completed_at": _ts(3)}))
    return brain


class GilTicker:
    """Counts how often a second thread gets the GIL while the build runs."""

    def __init__(self):
        self.ticks, self.max_gap, self._stop = 0, 0.0, False

    def run(self):
        last = time.perf_counter()
        while not self._stop:
            time.sleep(0.01)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last)
            last = now
            self.ticks += 1

    def __enter__(self):
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()
        return self

    def __exit__(self, *a):
        self._stop = True
        self.t.join(5)


class Probes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v2-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.brain = make_brain(self.tmp)
        self.r = SecretRedactor()

    def build(self, **kw):
        return build_office_state(brain_root=self.brain, redactor=self.r, now=NOW, **kw)

    # P1: quadratic backtracking in the handoff [next] regex (one untrusted file)
    def test_p1_handoff_obs_line_regex_is_not_quadratic(self):
        line = "- [next] x" + " " * 30000 + "y"
        (self.brain / "handoff" / "current.md").write_text("# T\n" + line + "\n")
        with GilTicker() as g:
            t0 = time.perf_counter()
            state = self.build()
            took = time.perf_counter() - t0
        print(f"\n[P1] build with 30k-space [next] line: {took:.2f}s, "
              f"longest GIL starvation of another thread: {g.max_gap:.2f}s")
        self.assertIsNotNone(state["handoff"])
        self.assertLess(took, 1.0, "one handoff line stalls the snapshot (and the GIL)")

    # P2: same for the H1 title regex
    def test_p2_handoff_h1_regex_is_not_quadratic(self):
        (self.brain / "handoff" / "current.md").write_text("# x" + " " * 30000 + "y\n- [status] done\n")
        t0 = time.perf_counter()
        self.build()
        took = time.perf_counter() - t0
        print(f"\n[P2] build with 30k-space H1: {took:.2f}s")
        self.assertLess(took, 1.0)

    # P3: handoff with an out-of-range mtime (btrfs/tmpfs store 64-bit seconds)
    def test_p3_handoff_extreme_mtime_does_not_raise(self):
        p = self.brain / "handoff" / "current.md"
        p.write_text("# T\n- [status] done\n- [agent] cline\n- [next] go\n")
        try:
            os.utime(p, (1e15, 1e15))
        except OSError as exc:
            self.skipTest(f"filesystem refuses the mtime: {exc}")
        self.assertGreater(p.stat().st_mtime, 1e14)
        state = self.build()  # must not raise (route would 500 on every poll)
        self.assertEqual(state["handoff"]["status"], "done")

    # P4: routing-history record with an unhashable job_id
    def test_p4_routing_history_unhashable_job_id(self):
        hist = [{"job_id": ["x"], "timestamp": _ts(1), "reason": "r", "selected_account": "a"},
                {"job_id": {"a": 1}, "timestamp": _ts(2)}]
        state = self.build(routing_history=hist)  # must not raise
        self.assertIsInstance(state["flows"], list)

    # P5: handoff/current.md is a FIFO: must not block the request (and the cache lock)
    def test_p5_handoff_fifo_does_not_block(self):
        os.mkfifo(self.brain / "handoff" / "current.md")
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("s", self.build()), daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "build blocked on a FIFO handoff")
        self.assertIsNone(out["s"]["handoff"]["title"])

    # P6: a directory (and a dangling symlink) named *.json in a state folder
    def test_p6_directory_and_dangling_link_named_json(self):
        c = self.brain / "swarm" / "tasks" / "completed"
        (c / "evil.json").mkdir()
        (c / "dangling.json").symlink_to(self.tmp / "does-not-exist.json")
        state = self.build()
        self.assertIn("task-ok", {t["id"] for t in state["tasks"]})
        self.assertGreaterEqual(state["sources"]["skipped_files"], 2)

    # P7: secrets in handoff [next]/[agent], memory scope, routing reason, account id
    def test_p7_secrets_in_non_task_sources_are_redacted(self):
        (self.brain / "handoff" / "current.md").write_text(
            f"---\ntitle: current\n---\n# Use {KEY}\n- [status] in-progress {KEY}\n"
            f"- [agent] <script>{KEY}</script>\n- [next] export OPENAI_API_KEY={KEY}\n")
        mem = [mock.Mock(scope=f"scope {KEY}", created_at=_ts(1), source_agent=f"a {KEY}", task_id=KEY)]
        ms = mock.Mock(count=lambda: 1, list_all=lambda limit=10: mem)
        acc = mock.Mock(id=f"acct-{KEY}", provider_id="openai", account_type="api",
                        display_name=f"Bearer {'q' * 20}", enabled=True, status="HEALTHY")
        hist = [{"timestamp": _ts(1), "reason": f"picked {KEY}", "selected_account": KEY, "job_id": "j1"}]
        state = self.build(memory_store=ms, accounts=[acc], routing_history=hist)
        body = json.dumps(state)
        self.assertNotIn(KEY, body)
        self.assertNotIn("q" * 20, body)
        self.assertNotIn(KEY[:20], body)

    # P8: lone surrogate / NUL / RTL override in fields: route must still answer 200 JSON
    def test_p8_hostile_unicode_is_serialisable(self):
        (self.brain / "swarm" / "tasks" / "pending" / "task-u.json").write_text(
            '{"id": "task-u\\u0000", "title": "\\ud800\\u202e<b>x</b>\\udfff", "assigned_to": "\\ud83d",'
            ' "created_at": "2026-09-24T11:00:00Z", "rationale": "\\u0000\\u0001"}')
        state = self.build()
        out = json.dumps(state)  # dashboard._serve_json uses the default ensure_ascii=True
        out.encode("utf-8")
        self.assertIn("task-u", out)

    # P9: 20000-entry folders x 4 states: build time must fit the 3s poll
    def test_p9_scan_cost_at_the_new_entry_cap(self):
        base = self.brain / "swarm" / "tasks"
        n = 20000
        for s in ("pending", "completed"):
            d = base / s
            for i in range(n):
                (d / f"t-{s}-{i}.json").write_bytes(b"{}")
        t0 = time.perf_counter()
        state = self.build()
        took = time.perf_counter() - t0
        print(f"\n[P9] build over 2 x {n} entries: {took:.2f}s, truncated_dirs={state['sources']['truncated_dirs']}")
        self.assertLess(took, 2.0)

    # P10: the 16 MiB budget is spent in folder order: big completed files starve escalated/
    def test_p10_budget_does_not_starve_escalated(self):
        c = self.brain / "swarm" / "tasks" / "completed"
        pad = "o" * (900 * 1024)
        for i in range(20):
            (c / f"task-big{i}.json").write_text(json.dumps({
                "id": f"task-big{i}", "title": "big", "assigned_to": "antigravity", "status": "completed",
                "created_at": _ts(30), "completed_at": _ts(29), "output": pad}))
        (self.brain / "swarm" / "tasks" / "escalated" / "task-esc.json").write_text(json.dumps({
            "id": "task-esc", "title": "failed now", "assigned_to": "cline", "status": "escalated",
            "created_at": _ts(3), "completed_at": _ts(1), "error": "boom"}))
        state = self.build()
        ids = {t["id"] for t in state["tasks"]}
        cline = next(a for a in state["agents"] if a["id"] == "cline")
        print(f"\n[P10] escalated present={'task-esc' in ids} cline status={cline['status']} "
              f"skipped={state['sources']['skipped_files']}")
        self.assertIn("task-esc", ids, "a fresh escalation vanished because older completed files spent the budget")


class RouteProbes(unittest.TestCase):
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

    # P11: auth variants that must never yield the snapshot
    def test_p11_auth_bypass_attempts(self):
        self.d._office_cache.clear()
        bad = [
            ("GET", "/api/office/state?x=1", {}),
            ("GET", "/api/office/state#frag", {}),
            ("GET", "//api/office/state", {}),
            ("GET", "/static/../api/office/state", {}),
            ("GET", "/static/..%2f..%2fapi/office/state", {}),
            ("GET", "/api/oauth/callback/../../office/state", {}),
            ("GET", "/api/office/state", {"Authorization": f"Basic {self.token}"}),
            ("GET", "/api/office/state", {"Authorization": f"Bearer {self.token}x"}),
            ("GET", "/api/office/state", {"Authorization": f"Bearer {self.token[:-1]}"}),
            ("GET", "/api/office/state", {"Authorization": "Bearer "}),
            ("GET", "/api/office/state", {"Cookie": f"token={self.token}"}),
            ("HEAD", "/api/office/state", {}),
            ("POST", "/api/office/state", {"Content-Length": "0"}),
        ]
        for method, path, headers in bad:
            status, body = self.req(method, path, headers)
            self.assertNotIn('"flows"', body, f"{method} {path} {headers} leaked the snapshot ({status})")
            if method == "GET" and path.startswith("/api/office/state"):
                self.assertEqual(status, 401, f"{method} {path} {headers}")

    # P12: poisoned handoff through the real route: bounded latency for other endpoints
    def test_p12_poisoned_handoff_does_not_freeze_other_endpoints(self):
        tmp = Path(tempfile.mkdtemp(prefix="v2r-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        brain = make_brain(tmp)
        (brain / "handoff" / "current.md").write_text("# T\n- [next] x" + " " * 30000 + "y\n")
        self.d._office_cache.clear()
        auth = {"Authorization": f"Bearer {self.token}"}
        lat = {}
        with mock.patch.dict(os.environ, {"BRAIN_DIR": str(brain)}):
            th = threading.Thread(target=lambda: lat.setdefault("office", self.req("GET", "/api/office/state", auth)))
            th.start()
            worst = 0.0
            while th.is_alive():
                t0 = time.perf_counter()
                self.req("GET", "/api/health")
                worst = max(worst, time.perf_counter() - t0)
                time.sleep(0.05)
            lat["health"] = worst
            th.join(120)
        print(f"\n[P12] worst /api/health latency while office snapshot builds: {lat['health']:.2f}s; office status {lat['office'][0]}")
        self.assertLess(lat["health"], 1.0, "one brain file freezes the whole dashboard process")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class KnownSecretProbe(unittest.TestCase):
    # P13: a registered (known) secret containing a whitespace run is collapsed
    # before redaction, so the exact-match known-secret check can no longer see it.
    def test_p13_known_secret_with_whitespace_run(self):
        r = SecretRedactor()
        secret = "hunter2  Tr0ub4dor"  # two spaces
        r.register_secret(secret)
        self.assertNotIn(secret, r.redact(f"pw {secret}"))  # the redactor itself catches it
        out = office_state.clean(f"pw {secret}", r, 140)
        print(f"\n[P13] clean() output: {out!r}")
        self.assertNotIn("Tr0ub4dor", out)
