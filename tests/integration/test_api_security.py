"""
Integration tests for API Security, Token Authentication, and Secret Redaction.
"""

import unittest
import urllib.request
import urllib.error
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import ui.dashboard.dashboard as dashboard
from tasks.manager import TaskManager
from ui.dashboard.dashboard import ThreadedHTTPServer, MissionControlHandler


class TestApiSecurity(unittest.TestCase):
    """Verify endpoint authentication, forbidden mutations without auth, and zero secret leaks."""

    @classmethod
    def setUpClass(cls):
        # BUG-003 fix (full-system validation): the dashboard module binds the
        # repo's real TaskManager/event bus at import time, and job submission
        # spawns orchestrator.execute_next() threads that drain whatever queue
        # is wired into the swarm. Redirect every persistent handle to a tmp
        # dir so tests can neither pollute nor execute production tasks.
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._orig_tm = dashboard.task_manager
        cls._orig_orch_tm = dashboard.orchestrator._task_manager
        cls._orig_swarm_tm = dashboard.orchestrator._swarm._task_manager
        cls._test_tm = TaskManager(root_tasks_dir=Path(cls._tmp_dir.name) / "tasks")
        dashboard.task_manager = cls._test_tm
        dashboard.orchestrator._task_manager = cls._test_tm
        dashboard.orchestrator._swarm._task_manager = cls._test_tm
        cls._orig_token_env = os.environ.get("MISSION_CONTROL_AUTH_TOKEN")
        os.environ["MISSION_CONTROL_AUTH_TOKEN"] = "test-suite-token-isolation-0123456789"

        # Ephemeral port: a fixed port collides with any concurrently running suite.
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), MissionControlHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

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

    def test_token_endpoint_works_and_returns_token(self):
        """Verify /api/token returns a session bearer token."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/token")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("token", data)
            self.assertTrue(len(data["token"]) > 10)

    def test_unauthenticated_mutation_is_rejected(self):
        """Mutating POST/PATCH/DELETE endpoints without bearer token must return 401."""
        endpoints = [
            f"http://127.0.0.1:{self.port}/api/jobs",
            f"http://127.0.0.1:{self.port}/api/providers",
            f"http://127.0.0.1:{self.port}/api/accounts"
        ]
        for url in endpoints:
            req = urllib.request.Request(
                url,
                data=json.dumps({"test": "data"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 401)

    def test_authenticated_requests_succeed(self):
        """With valid bearer token, authorized requests succeed."""
        # Get token
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/token") as resp:
            token = json.loads(resp.read().decode("utf-8"))["token"]

        # Submit job with auth
        # auto_execute=False: this checks authorisation, not execution. Left on,
        # the job is run for real in a background thread against a live agent CLI.
        payload = json.dumps(
            {"task": "Run security check", "priority": 5, "auto_execute": False}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/jobs",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}"
            }
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("job", data)

    def test_zero_leakage_in_api_responses(self):
        """Verify status and accounts endpoints never contain secret keys in raw text."""
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status") as resp:
            raw_text = resp.read().decode("utf-8")
            self.assertNotIn("sk-proj-", raw_text)
            self.assertNotIn("api_key", raw_text.lower())

        # /api/accounts is not public: authenticate as the browser UI does, so
        # the leakage check runs against the real authorised payload.
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/token") as resp:
            token = json.loads(resp.read().decode("utf-8"))["token"]
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/accounts",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            raw_text = resp.read().decode("utf-8")
            self.assertNotIn("sk-proj-", raw_text)
            self.assertNotIn("my-super-secret", raw_text)


if __name__ == "__main__":
    unittest.main()
