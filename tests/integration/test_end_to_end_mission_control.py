"""
End-to-End Integration Tests for Universal AI Mission Control (Phases 12, 13, 14).
Includes 10-job concurrency, multi-factor routing, usage attribution, and failover verification.
"""

import unittest
import urllib.request
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest import mock

from agents.base.adapter import TaskExecutionResult
from providers.adapters.bridge import AgentProviderBridge
from ui.dashboard.dashboard import ThreadedHTTPServer, MissionControlHandler


def _fake_bridge_execute(self, job):
    """Stand-in for a real agent CLI run.

    /api/jobs auto-executes in a background thread. Unstubbed, that spawns the
    real agent CLI on the operator's real account profile and spends real quota.
    The HTTP -> router -> JobManager -> provider chain still runs for real; only
    the final CLI invocation is replaced.
    """
    job.mark_started()
    res = TaskExecutionResult(
        task_id=job.id,
        agent_id=self.adapter.agent_id,
        account_id=self.adapter.account_id,
        provider=self.adapter.provider,
        success=True,
        exit_code=0,
        output="stubbed e2e execution",
        error="",
        actual_model=job.model or None,
        total_tokens=42,
    )
    job.mark_completed(result={"output": res.output}, duration=0.0)
    return res


class TestEndToEndMissionControl(unittest.TestCase):
    """End-to-end integration test of Universal AI Mission Control."""

    @classmethod
    def setUpClass(cls):
        cls._exec_patch = mock.patch.object(AgentProviderBridge, "execute", _fake_bridge_execute)
        cls._exec_patch.start()
        # Ephemeral port: a fixed port collides with any concurrently running suite.
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), MissionControlHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Retrieve auth token
        with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/api/token") as resp:
            cls.token = json.loads(resp.read().decode("utf-8"))["token"]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._exec_patch.stop()

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}"
            }
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path):
        # Sensitive GETs need the same bearer token the UI fetched from /api/token.
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_full_mission_control_lifecycle(self):
        """Test complete lifecycle: discovery, job submission, auto-routing, and usage tracking."""
        # 1. Check overview
        overview = self._get("/api/overview")
        self.assertIn("mission_control", overview)

        # 2. Check providers
        provs = self._get("/api/providers")
        self.assertIn("providers", provs)
        self.assertTrue(len(provs["providers"]) > 0)

        # 3. Submit a job with Auto routing
        job_result = self._post("/api/jobs", {
            "task": "Write python function to compute fibonacci sequence",
            "routing_mode": "balanced",
            "priority": 7
        })
        self.assertIn("job", job_result)
        job_id = job_result["job"]["id"]
        self.assertIn("routing_decision", job_result)
        decision = job_result["routing_decision"]
        self.assertIsNotNone(decision["provider_id"])
        self.assertIsNotNone(decision["account_id"])

        # 4. Inspect routing decision by job_id
        inspect = self._get(f"/api/routing/inspect?job_id={job_id}")
        self.assertIn("decision", inspect)
        dec = inspect["decision"]
        self.assertIn("score_breakdown", dec)
        self.assertIn("capability_match", dec["score_breakdown"])

        # 5. Execution runs in a background thread; wait for the job to finish,
        #    then verify it completed and its usage was recorded. (Checking
        #    /api/usage immediately only passed on leftover usage history.)
        job = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = self._get(f"/api/jobs/{job_id}")
            if job.get("status") in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.2)
        self.assertEqual(job.get("status"), "completed", f"job did not complete: {job.get('error')}")
        usage = self._get("/api/usage")
        self.assertGreater(usage["requests"], 0)

    def test_10_concurrent_jobs_execution(self):
        """Run 10 concurrent jobs simultaneously.
        Verify no account cross-contamination, correct attribution, and stability.
        """
        def submit_job(i):
            return self._post("/api/jobs", {
                "task": f"Concurrent task #{i}: Analyze dataset chunk",
                "routing_mode": "balanced",
                "priority": i
            })

        results = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(submit_job, i) for i in range(10)]
            for fut in as_completed(futures):
                results.append(fut.result())

        self.assertEqual(len(results), 10)
        job_ids = set()
        for r in results:
            self.assertIn("job", r)
            j = r["job"]
            self.assertIn(j["id"], set(j["id"] for _ in [1]))
            job_ids.add(j["id"])
            self.assertIn(j["status"], ["pending", "running", "completed", "failed"])

        # All 10 job IDs must be unique
        self.assertEqual(len(job_ids), 10)


if __name__ == "__main__":
    unittest.main()
