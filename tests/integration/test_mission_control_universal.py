"""Integration tests for Universal AI Mission Control CLI and End-to-End Orchestration."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.hermetic import FIXTURE_CONFIG

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CONFIG = PROJECT_ROOT / "config" / "providers.json"

#: Runs scripts/brain.py in a child interpreter *after* importing the ``tests``
#: package, so the child gets the same hermetic defaults as this process
#: (temp repo state, temp credential store with the keyring disabled, no real
#: agent CLI runs). Invoked directly, the child wrote runtime/audit/audit.jsonl
#: and memory/store/shared_memory.db into the checkout and stored the test's
#: fake secret in the operator's real credential store.
_HERMETIC_CLI_BOOTSTRAP = (
    "import runpy, sys, tests; sys.argv = sys.argv[1:]; "
    "runpy.run_path(sys.argv[0], run_name='__main__')"
)


class TestMissionControlUniversal(unittest.TestCase):
    """CLI integration tests.

    These exercise commands that add, disable, enable and remove accounts, all of
    which persist to the provider configuration. They therefore run against a
    throwaway copy selected with BRAIN_PROVIDERS_CONFIG: writing to the canonical
    config/providers.json would mutate the operator's real configuration and make
    the rest of the suite order-dependent on whichever providers survived.
    """

    def setUp(self) -> None:
        self._tmp_dir = tempfile.mkdtemp(prefix="mission-control-config-")
        self.config_path = Path(self._tmp_dir) / "providers.json"
        # Start from the test fixture, not the operator's live account list.
        shutil.copy2(FIXTURE_CONFIG, self.config_path)
        self._canonical_bytes = CANONICAL_CONFIG.read_bytes()

    def tearDown(self) -> None:
        # The canonical configuration must be byte-identical afterwards.
        self.assertEqual(
            CANONICAL_CONFIG.read_bytes(),
            self._canonical_bytes,
            "a CLI integration test mutated the canonical config/providers.json",
        )
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def run_cli(self, args: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["BRAIN_PROVIDERS_CONFIG"] = str(self.config_path)
        # Keep this test's config copy across CLI calls (lifecycle persistence).
        env["BRAIN_TESTS_KEEP_CONFIG"] = "1"
        cmd = [
            sys.executable, "-c", _HERMETIC_CLI_BOOTSTRAP,
            str(PROJECT_ROOT / "scripts" / "brain.py"),
        ] + args
        return subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, env=env
        )

    def test_accounts_list_cli(self):
        res = self.run_cli(["accounts", "list"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("ACCOUNT", res.stdout)
        self.assertIn("PROVIDER", res.stdout)
        self.assertIn("antigravity-account-1", res.stdout)
        self.assertIn("kiro-cli", res.stdout)
        self.assertIn("cline", res.stdout)

        # JSON output mode
        res_json = self.run_cli(["accounts", "list", "--json"])
        self.assertEqual(res_json.returncode, 0)
        accounts = json.loads(res_json.stdout)
        self.assertIsInstance(accounts, list)
        self.assertTrue(any(a["id"] == "antigravity-account-1" for a in accounts))

    def test_accounts_inspect_cli(self):
        res = self.run_cli(["accounts", "inspect", "antigravity-account-1"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("=== Account Inspection: antigravity-account-1 ===", res.stdout)
        self.assertIn("Provider:             antigravity", res.stdout)
        self.assertIn("Authentication:       oauth", res.stdout)

    def test_accounts_add_enable_disable_remove_lifecycle(self):
        test_acct_name = "integration-test-acct"
        # 1. Add account
        res_add = self.run_cli([
            "accounts", "add",
            "--provider", "openai",
            "--name", test_acct_name,
            "--auth-type", "API Key",
            "--secret", "sk-mock-integration-secret-9999",
            "--models", "gpt-4o,gpt-4o-mini",
        ])
        self.assertEqual(res_add.returncode, 0)
        self.assertIn("Successfully added account", res_add.stdout)

        # Verify no plaintext secret leaked to stdout
        self.assertNotIn("sk-mock-integration-secret-9999", res_add.stdout)

        acct_full_id = f"openai-{test_acct_name}"

        # 2. Inspect added account
        res_insp = self.run_cli(["accounts", "inspect", acct_full_id, "--json"])
        self.assertEqual(res_insp.returncode, 0)
        data = json.loads(res_insp.stdout)
        self.assertEqual(data["id"], acct_full_id)
        self.assertEqual(data["provider_id"], "openai")
        self.assertTrue(data["enabled"])
        self.assertIn("secret://mission-control/openai/", data["credential_reference"])
        self.assertNotIn("sk-mock-integration-secret-9999", json.dumps(data))

        # 3. Disable account
        res_dis = self.run_cli(["accounts", "disable", acct_full_id])
        self.assertEqual(res_dis.returncode, 0)

        # Verify disabled in list
        res_list = self.run_cli(["accounts", "list"])
        self.assertEqual(res_list.returncode, 0)
        for line in res_list.stdout.splitlines():
            if acct_full_id in line:
                self.assertIn("DISABLED", line)

        # 4. Enable account
        res_en = self.run_cli(["accounts", "enable", acct_full_id])
        self.assertEqual(res_en.returncode, 0)

        # 5. Remove account with confirmation
        res_rem = self.run_cli(["accounts", "remove", acct_full_id, "--confirm"])
        self.assertEqual(res_rem.returncode, 0)
        self.assertIn("removed", res_rem.stdout)

        # No config cleanup needed: this test operates on a temp copy selected by
        # BRAIN_PROVIDERS_CONFIG, which tearDown discards. Deleting the openai
        # provider from the canonical config here is what previously made the
        # suite order-dependent.

    def test_providers_list_and_health_cli(self):
        res_list = self.run_cli(["providers", "list"])
        self.assertEqual(res_list.returncode, 0)
        self.assertIn("antigravity", res_list.stdout)
        self.assertIn("kiro", res_list.stdout)
        self.assertIn("cline", res_list.stdout)

        res_hlth = self.run_cli(["providers", "health"])
        self.assertEqual(res_hlth.returncode, 0)
        self.assertIn("ONLINE", res_hlth.stdout)

    def test_job_submit_and_inspect_cli(self):
        res_sub = self.run_cli([
            "job", "submit",
            "--task", "Smoke test verification",
            "--provider", "antigravity",
            "--worker", "antigravity-account-1",
        ])
        self.assertEqual(res_sub.returncode, 0)
        self.assertIn("Job created:", res_sub.stdout)

        job_id = None
        for line in res_sub.stdout.splitlines():
            if "Job created:" in line:
                job_id = line.split("Job created:")[1].strip()
                break
        self.assertIsNotNone(job_id)

        # Inspect job
        res_insp = self.run_cli(["job", "inspect", job_id, "--json"])
        self.assertEqual(res_insp.returncode, 0)
        job_data = json.loads(res_insp.stdout)
        self.assertEqual(job_data["id"], job_id)
        self.assertEqual(job_data["provider"], "antigravity")
        self.assertEqual(job_data["status"], "pending")

        # Cleanup job file
        job_file = PROJECT_ROOT / "tasks" / "jobs" / f"{job_id}.json"
        if job_file.is_file():
            job_file.unlink()


if __name__ == "__main__":
    unittest.main()
