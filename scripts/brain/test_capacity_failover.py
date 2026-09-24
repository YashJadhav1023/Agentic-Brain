#!/usr/bin/env python3
"""Prove the antigravity capacity failover now downgrades the model.

Before the fix the model id was chosen once outside the account loop, so a
capacity wall on one id was retried on the other account with the *same* id and
then escalated. This drives the real execute path with a stubbed subprocess so
the branch is exercised without consuming quota.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swarm  # noqa: E402

CAPACITY_MSG = "Error: RESOURCE_EXHAUSTED: quota exceeded for this model"


class FakeProc:
    def __init__(self, stdout, stderr, returncode):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def run_case(name, failure, expect_models, expect_status, expect_call_count=None):
    """Drive execute_task_worker with a stubbed subprocess.

    ``failure`` maps a model id to the (stderr, exit_code) it should fail with.
    Any model not in the map succeeds.
    """
    calls: list[str] = []

    def fake_run(cmd, **kwargs):
        model = next((a.split("=", 1)[1] for a in cmd if a.startswith("--model=")), "")
        calls.append(model)
        if model in failure:
            stderr, code = failure[model]
            return FakeProc("", stderr, code)
        return FakeProc("STUB_OK", "", 0)

    original = subprocess.run
    swarm.subprocess.run = fake_run
    try:
        task = {
            "id": "task-stub",
            "title": "Summarize one line about bulkheads",
            "description": "",
            "assigned_to": "antigravity-api",
            "status": "pending",
            "repo": "",
        }
        swarm.execute_task_worker(task)
    finally:
        swarm.subprocess.run = original

    ok_model = task.get("model") in expect_models
    ok_status = task.get("status") == expect_status
    ok_count = expect_call_count is None or len(calls) == expect_call_count
    verdict = "PASS" if (ok_model and ok_status and ok_count) else "FAIL"
    print(f"[{verdict}] {name}")
    print(f"         models tried : {calls}")
    print(f"         final model  : {task.get('model')}  (expected one of {expect_models})")
    print(f"         final status : {task.get('status')}  (expected {expect_status})")
    if expect_call_count is not None:
        print(f"         call count   : {len(calls)}  (expected {expect_call_count})")
    print(f"         attempts     : {task.get('antigravity_attempts')}")
    print()
    return ok_model and ok_status and ok_count


ALL_GEMINI = (
    "gemini-3.8-flash-medium",
    "gemini-3.7-flash-high",
    "gemini-3.7-flash-medium",
    "gemini-3.8-flash-low",
    "gemini-3.7-flash-low",
)


def capacity_on(*models):
    return {m: (CAPACITY_MSG, 1) for m in models}


results = []

# The exact 2026-09-21 failure shape: a capacity wall on the model the policy
# actually picked. Previously this escalated the task; it must now downgrade.
results.append(
    run_case(
        "capacity on the chosen model downgrades to the next candidate",
        failure=capacity_on("gemini-3.7-flash-medium"),
        expect_models={m for m in ALL_GEMINI if m != "gemini-3.7-flash-medium"},
        expect_status="completed",
    )
)

# Deep wall within the cap: the last model inside the attempt budget answers.
results.append(
    run_case(
        "capacity walks down to the last model inside the attempt cap",
        failure=capacity_on("gemini-3.7-flash-medium", "gemini-3.8-flash-low", "gemini-3.8-flash-medium"),
        expect_models={"gemini-3.7-flash-high", "gemini-3.7-flash-low"},
        expect_status="completed",
        expect_call_count=swarm.MAX_MODEL_ATTEMPTS_PER_ACCOUNT,
    )
)

# The cap itself: with every model walled, the walk must be bounded to
# cap x accounts rather than serially burning the whole ranked catalogue.
# Capacity errors return in seconds, so this bound is about not queueing up an
# unbounded serial retry, not about the wall clock of a healthy run.
ACCOUNTS = len(swarm.antigravity_accounts("antigravity-api"))
results.append(
    run_case(
        "attempt cap bounds the walk when every model is walled",
        failure=capacity_on(*ALL_GEMINI),
        expect_models=set(ALL_GEMINI),
        expect_status="escalated",
        expect_call_count=swarm.MAX_MODEL_ATTEMPTS_PER_ACCOUNT * ACCOUNTS,
    )
)

# The guard that matters: a diagnosable task error is NOT a capacity wall, so it
# must stop after a single attempt instead of burning every model in the pool.
results.append(
    run_case(
        "a genuine task error stops after one attempt, no downgrade",
        failure={m: ("Traceback: the task itself is broken", 1) for m in ALL_GEMINI},
        expect_models=set(ALL_GEMINI),
        expect_status="escalated",
        expect_call_count=1,
    )
)

print("ALL PASS" if all(results) else "FAILURES PRESENT")
sys.exit(0 if all(results) else 1)

