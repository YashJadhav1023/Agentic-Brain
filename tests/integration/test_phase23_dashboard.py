"""Integration tests for Phase 23: Dashboard security hardening, Bearer auth on all endpoints,
SSE dual-mode auth, host binding validation, and coexistence with active ECC federation.
"""
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from brain.mission_brain import MissionBrain
from brain.resources.resource_model import ResourceLifecycleState
from tasks.manager import TaskManager
from ui.dashboard import dashboard
from ui.dashboard.dashboard import SecurityError, validate_host_binding


class TestPhase23DashboardSecurity(unittest.TestCase):
    """Integration test suite for Dashboard auth, loopback enforcement, and ECC federation."""

    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._orig_tm = dashboard.task_manager
        cls._orig_orch_tm = dashboard.orchestrator._task_manager
        cls._orig_swarm_tm = dashboard.orchestrator._swarm._task_manager
        cls._test_tm = TaskManager(root_tasks_dir=Path(cls._tmp_dir.name) / "tasks")
        dashboard.task_manager = cls._test_tm
        dashboard.orchestrator._task_manager = cls._test_tm
        dashboard.orchestrator._swarm._task_manager = cls._test_tm

        cls._orig_token_env = os.environ.get("MISSION_CONTROL_AUTH_TOKEN")
        cls.test_token = "p23-test-token-hardening-sec-998877"
        os.environ["MISSION_CONTROL_AUTH_TOKEN"] = cls.test_token

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
        timeout: float = 10.0,
    ) -> tuple[int, dict, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req_headers = dict(headers or {})
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = dict(resp.headers)
                raw = resp.read().decode("utf-8")
                try:
                    resp_body = json.loads(raw) if raw else {}
                except Exception:
                    resp_body = {"_raw": raw}
                return resp.status, resp_headers, resp_body
        except urllib.error.HTTPError as exc:
            resp_headers = dict(exc.headers)
            try:
                resp_body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                resp_body = {}
            return exc.code, resp_headers, resp_body

    def test_public_get_endpoints_accessible_without_token(self):
        """Public GET endpoints must return 200 without Authorization header."""
        for path in ["/", "/api/health", "/api/status", "/api/token"]:
            status, headers, body = self._request(path)
            self.assertEqual(status, 200, f"Expected 200 for public path: {path}")

    def test_sensitive_get_endpoints_require_token(self):
        """All non-public GET endpoints must return 401 without Bearer token."""
        sensitive_paths = [
            "/api/overview",
            "/api/agents",
            "/api/sessions",
            "/api/tasks",
            "/api/accounts",
            "/api/providers",
            "/api/usage",
            "/api/cost",
            "/api/steering",
            "/api/events",
        ]
        for path in sensitive_paths:
            status, headers, body = self._request(path)
            self.assertEqual(
                status,
                401,
                f"Path {path} should require auth but returned {status}",
            )
            self.assertIn("WWW-Authenticate", headers)

    def test_sensitive_get_endpoints_succeed_with_valid_bearer_token(self):
        """All sensitive GET endpoints must return 200 when presented with valid Bearer token."""
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        sensitive_paths = [
            "/api/overview",
            "/api/agents",
            "/api/sessions",
            "/api/tasks",
            "/api/accounts",
            "/api/providers",
            "/api/usage",
            "/api/cost",
            "/api/steering",
            "/api/events",
        ]
        for path in sensitive_paths:
            status, headers, body = self._request(path, headers=auth_headers)
            self.assertEqual(
                status,
                200,
                f"Path {path} with valid auth failed: {status}, body={body}",
            )

    def test_sse_dual_mode_auth(self):
        """SSE stream endpoint /api/events/stream allows both Bearer header and query param token, rejecting bad tokens."""
        # 1. Unauthenticated SSE -> 401
        status, _, _ = self._request("/api/events/stream")
        self.assertEqual(status, 401)

        # 2. Query param with invalid token -> 401
        status, _, _ = self._request("/api/events/stream?token=invalid-garbage-token")
        self.assertEqual(status, 401)

        # 3. Query param with valid token -> accepts and establishes stream
        # Connect via raw socket to read initial SSE handshake headers without hanging
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(("127.0.0.1", self.port))
        req_line = f"GET /api/events/stream?token={self.token} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n"
        s.sendall(req_line.encode("utf-8"))
        data = s.recv(1024).decode("utf-8")
        s.close()
        self.assertIn("HTTP/1.0 200 OK", data if "HTTP/1.0" in data else "HTTP/1.1 200 OK")
        self.assertIn("text/event-stream", data)

        # 4. Bearer header for SSE stream
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect(("127.0.0.1", self.port))
        req_line = f"GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nAuthorization: Bearer {self.token}\r\n\r\n"
        s.sendall(req_line.encode("utf-8"))
        data = s.recv(1024).decode("utf-8")
        s.close()
        self.assertTrue("200 OK" in data)
        self.assertIn("text/event-stream", data)

    def test_query_token_forbidden_on_non_sse_endpoints(self):
        """Query parameter token auth must NOT be allowed on normal GET endpoints (only SSE)."""
        status, _, _ = self._request(f"/api/overview?token={self.token}")
        self.assertEqual(status, 401)

    def test_host_binding_validation_enforces_loopback(self):
        """Host binding validator must allow loopback and reject external interfaces."""
        self.assertEqual(validate_host_binding("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_host_binding("localhost"), "localhost")
        self.assertEqual(validate_host_binding("::1"), "::1")

        with self.assertRaises(SecurityError):
            validate_host_binding("0.0.0.0")

        with self.assertRaises(SecurityError):
            validate_host_binding("192.168.1.50")

        with self.assertRaises(SecurityError):
            validate_host_binding("public.domain.com")

    def test_dashboard_coexists_with_active_ecc_federation(self):
        """Verify dashboard operates normally when ECC federation is active and managing capabilities."""
        mb = MissionBrain()
        fed = mb.ecc_federation
        report = fed.federate()
        total_registered = sum(report.values())
        self.assertGreater(total_registered, 0)

        # Dashboard status still 200 with Bearer
        auth_headers = {"Authorization": f"Bearer {self.token}"}
        status, _, body = self._request("/api/overview", headers=auth_headers)
        self.assertEqual(status, 200)

        # Enable a Category A capability in federation
        target_id = "ecc:skill:react-performance"
        success = fed.enable(target_id)
        self.assertTrue(success)

        # Dashboard remains responsive and authenticated
        status, _, body = self._request("/api/status")
        self.assertEqual(status, 200)
        self.assertIn(body.get("status"), ["RUNNING", "operational"])

        # Cleanup: emergency kill switch
        fed.disable_all()

    def test_delete_and_cancel_task_endpoints(self):
        """Verify cancel and permanent delete endpoints operate securely and correctly."""
        from tasks.manager import TaskStatus

        task = self._test_tm.create_task(title="cancel me", description="testing cancel and delete", task_id="task-integ-123")
        auth_headers = {"Authorization": f"Bearer {self.token}"}

        # 1. Unauthenticated DELETE returns 401
        status, _, _ = self._request("/api/task?task_id=task-integ-123", method="DELETE")
        self.assertEqual(status, 401)

        # 2. Authenticated cancel returns 200 and marks task cancelled
        status, _, body = self._request(
            "/api/task/cancel",
            method="POST",
            headers=auth_headers,
            data={"task_id": "task-integ-123"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "cancelled")
        reloaded = self._test_tm.get_task("task-integ-123")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.status, TaskStatus.CANCELLED)

        # 3. Authenticated DELETE returns 200 and purges task completely
        status, _, body = self._request(
            "/api/task?task_id=task-integ-123",
            method="DELETE",
            headers=auth_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "deleted")
        self.assertTrue(body.get("deleted"))

        # 4. GET now returns 404
        status, _, _ = self._request(
            "/api/task?task_id=task-integ-123",
            headers=auth_headers,
        )
        self.assertEqual(status, 404)
        self.assertIsNone(self._test_tm.get_task("task-integ-123"))
