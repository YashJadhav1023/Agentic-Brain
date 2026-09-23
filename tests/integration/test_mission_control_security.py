"""Integration tests for Mission Control security hardening, auth, CORS, and worktree endpoints."""
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

from events.bus import EventType
from ui.dashboard import dashboard


import tempfile
from tasks.manager import TaskManager


class TestMissionControlSecurity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Isolate tasks directory so test dispatches do not pollute production queue
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._orig_tm = dashboard.task_manager
        cls._orig_orch_tm = dashboard.orchestrator._task_manager
        cls._orig_swarm_tm = dashboard.orchestrator._swarm._task_manager
        cls._test_tm = TaskManager(root_tasks_dir=Path(cls._tmp_dir.name) / "tasks")
        dashboard.task_manager = cls._test_tm
        dashboard.orchestrator._task_manager = cls._test_tm
        # The swarm holds its own TaskManager reference and is the component
        # that actually executes queued tasks (BUG-003 fix): without swapping
        # it too, a background execute_next() thread would drain the repo queue.
        dashboard.orchestrator._swarm._task_manager = cls._test_tm
        # Pin the bearer token via env so tests never read/write the repo's
        # runtime/mission_control.token file.
        cls._orig_token_env = os.environ.get("MISSION_CONTROL_AUTH_TOKEN")
        os.environ["MISSION_CONTROL_AUTH_TOKEN"] = "test-suite-token-isolation-0123456789"

        # /api/dispatch defaults to auto_execute=True, which starts
        # orchestrator.execute_next() in a thread. The swapped TaskManager keeps
        # the queue isolated, but the tasks were still executed for real: the
        # live Antigravity CLI ran headless on account 2's real profile on every
        # suite run. These tests cover auth, CORS and rate limiting only, so
        # execution is replaced with a no-op that records it was triggered.
        cls._exec_patch = mock.patch.object(
            dashboard.orchestrator, "execute_next", return_value=[]
        )
        cls.execute_next = cls._exec_patch.start()

        # Bind ephemeral loopback port for testing
        cls.server = dashboard.ThreadedHTTPServer(
            ("127.0.0.1", 0), dashboard.MissionControlHandler
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.token = dashboard.get_or_create_auth_token()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._exec_patch.stop()
        dashboard.task_manager = cls._orig_tm
        dashboard.orchestrator._task_manager = cls._orig_orch_tm
        dashboard.orchestrator._swarm._task_manager = cls._orig_swarm_tm
        if cls._orig_token_env is None:
            os.environ.pop("MISSION_CONTROL_AUTH_TOKEN", None)
        else:
            os.environ["MISSION_CONTROL_AUTH_TOKEN"] = cls._orig_token_env
        cls._tmp_dir.cleanup()

    def _request(
        self,
        path: str,
        method: str = "GET",
        headers: dict | None = None,
        data: dict | None = None,
    ) -> tuple[int, dict, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req_headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_headers = dict(resp.headers)
                resp_body = json.loads(resp.read().decode("utf-8")) if resp.length != 0 else {}
                return resp.status, resp_headers, resp_body
        except urllib.error.HTTPError as exc:
            resp_headers = dict(exc.headers)
            try:
                resp_body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                resp_body = {}
            return exc.code, resp_headers, resp_body

    def test_security_headers_present_on_all_responses(self):
        status, headers, _ = self._request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("Content-Security-Policy", headers)

    def test_cors_allowed_local_origin(self):
        status, headers, _ = self._request(
            "/api/status", headers={"Origin": "http://127.0.0.1:3333"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://127.0.0.1:3333")
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers", ""))

    def test_cors_disallowed_origin_returns_403(self):
        status, _, body = self._request(
            "/api/status", headers={"Origin": "https://malicious-site.com"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body.get("error"), "Forbidden")

    def test_options_preflight(self):
        # Local origin preflight allowed
        status, headers, _ = self._request(
            "/api/dispatch",
            method="OPTIONS",
            headers={"Origin": "http://localhost:5173"},
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "http://localhost:5173")

        # Remote origin preflight rejected
        status, _, _ = self._request(
            "/api/dispatch",
            method="OPTIONS",
            headers={"Origin": "http://evil.com"},
        )
        self.assertEqual(status, 403)

    def test_safe_get_endpoints_unauthenticated(self):
        safe_endpoints = [
            "/api/status",
            "/api/health",
            "/api/token",
        ]
        for ep in safe_endpoints:
            status, _, _ = self._request(ep)
            self.assertEqual(status, 200, f"Safe endpoint {ep} failed without auth")

    def test_token_endpoint_returns_valid_token(self):
        status, _, body = self._request("/api/token")
        self.assertEqual(status, 200)
        self.assertIn("token", body)
        self.assertEqual(body["token"], self.token)

    def test_mutating_endpoints_require_bearer_auth(self):
        mutating = [
            ("/api/dispatch", {"instruction": "test"}),
            ("/api/continue", {}),
            ("/api/worktrees/approve", {"task_id": "test", "confirm": True}),
            ("/api/worktrees/reject", {"task_id": "test", "confirm": True}),
            ("/api/worktrees/cleanup", {"confirm": True}),
            ("/api/worktrees/recover", {"task_id": "test"}),
        ]
        for path, payload in mutating:
            status, _, body = self._request(path, method="POST", data=payload)
            self.assertEqual(status, 401, f"{path} should return 401 without auth")
            self.assertEqual(body.get("error"), "Unauthorized")

    def test_invalid_bearer_token_rejected_with_auth_failure_event(self):
        initial_events = len(dashboard.event_bus.get_recent_events(50))
        status, _, body = self._request(
            "/api/dispatch",
            method="POST",
            headers={"Authorization": "Bearer bad-token-invalid"},
            data={"instruction": "test invalid token"},
        )
        self.assertEqual(status, 401)
        self.assertEqual(body.get("error"), "Unauthorized")

        # Verify AUTH_FAILURE event was emitted
        recent = dashboard.event_bus.get_recent_events(10)
        auth_failures = [e for e in recent if e.event_type == EventType.AUTH_FAILURE]
        self.assertTrue(len(auth_failures) > 0)
        self.assertEqual(auth_failures[-1].metadata.get("path"), "/api/dispatch")

    def test_authenticated_dispatch_accepted_with_auth_success_event(self):
        status, _, body = self._request(
            "/api/dispatch",
            method="POST",
            headers={"Authorization": f"Bearer {self.token}"},
            data={"instruction": "Read status file and report findings"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "created")
        self.assertIn("task", body)

        recent = dashboard.event_bus.get_recent_events(50)
        auth_successes = [e for e in recent if e.event_type == EventType.AUTH_SUCCESS]
        self.assertTrue(len(auth_successes) > 0)

    def test_destructive_actions_require_explicit_confirmation(self):
        auth_headers = {"Authorization": f"Bearer {self.token}"}

        # Approve without confirm
        status, _, body = self._request(
            "/api/worktrees/approve",
            method="POST",
            headers=auth_headers,
            data={"task_id": "fake-task"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "Confirmation required")

        # Reject without confirm
        status, _, body = self._request(
            "/api/worktrees/reject",
            method="POST",
            headers=auth_headers,
            data={"task_id": "fake-task"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "Confirmation required")

        # Cleanup without confirm
        status, _, body = self._request(
            "/api/worktrees/cleanup",
            method="POST",
            headers=auth_headers,
            data={},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error"), "Confirmation required")

    def test_rate_limiting_on_execution_endpoint(self):
        dashboard.execution_rate_limiter.reset()
        orig_max = dashboard.execution_rate_limiter.max_requests
        dashboard.execution_rate_limiter.max_requests = 3
        auth_headers = {"Authorization": f"Bearer {self.token}"}

        try:
            # 3 requests should pass
            for _ in range(3):
                status, _, body = self._request(
                    "/api/dispatch",
                    method="POST",
                    headers=auth_headers,
                    data={"instruction": "task in rate limit"},
                )
                self.assertEqual(status, 200)

            # 4th request must hit 429
            status, _, body = self._request(
                "/api/dispatch",
                method="POST",
                headers=auth_headers,
                data={"instruction": "exceeded task"},
            )
            self.assertEqual(status, 429)
            self.assertEqual(body.get("error"), "Too Many Requests")
        finally:
            dashboard.execution_rate_limiter.max_requests = orig_max
            dashboard.execution_rate_limiter.reset()

    def test_worktree_endpoints_lifecycle(self):
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        task_id = "test-security-lifecycle-wt"

        # 1. Create worktree via orchestrator
        wt = dashboard.orchestrator.worktrees.create(
            task_id=task_id, agent_id="antigravity-account-1", account_id="account-1"
        )
        self.assertIsNotNone(wt)

        try:
            # 2. GET /api/worktrees
            auth_hdr = {"Authorization": f"Bearer {self.token}"}
            status, _, body = self._request("/api/worktrees", headers=auth_hdr)
            self.assertEqual(status, 200)
            self.assertIn("worktrees", body)
            matching = [w for w in body["worktrees"] if w["task_id"] == task_id]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["status"], "ACTIVE")

            # 3. GET /api/worktrees/diff
            status, _, diff_body = self._request(f"/api/worktrees/diff?task_id={task_id}", headers=auth_hdr)
            self.assertEqual(status, 200)
            self.assertIn("diff", diff_body)

            # 4. Reject worktree with confirm
            status, _, rej_body = self._request(
                "/api/worktrees/reject",
                method="POST",
                headers=auth_headers,
                data={"task_id": task_id, "confirm": True},
            )
            self.assertEqual(status, 200)
            self.assertEqual(rej_body.get("status"), "rejected")

            # 5. Verify events emitted
            recent = dashboard.event_bus.get_recent_events(10)
            event_types = [e.event_type for e in recent]
            self.assertIn(EventType.DIFF_REJECTED, event_types)
            self.assertIn(EventType.WORKTREE_DESTROYED, event_types)
        finally:
            # Cleanup worktree if still exists
            try:
                dashboard.orchestrator.worktrees.remove(task_id, confirm=True)
            except Exception:
                pass

    def test_secret_redaction_and_zero_leak(self):
        # Ensure that /api/status does not leak tokens or private keys
        status, _, body = self._request("/api/status")
        blob = json.dumps(body).lower()
        for forbidden in ("api_key", "apikey", "bearer ", "gemini_api_key", "password"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
