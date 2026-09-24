#!/usr/bin/env python3
"""Return an orphaned in-progress swarm task to the pending queue.

A task is orphaned when the runner that claimed it died before recording an
outcome, which leaves it in-progress forever with no process behind it.
"""

import json
import os
import pathlib
import sys

# Honour BRAIN_DIR like every other brain tool; a hardcoded ~/agentic-brain
# requeued tasks in a different store than the swarm that runs them.
BASE = pathlib.Path(os.environ.get("BRAIN_DIR") or pathlib.Path.home() / "agentic-brain") / "swarm" / "tasks"
RUN_FIELDS = (
    "started_at",
    "completed_at",
    "error",
    "output",
    "model",
    "effort",
    "antigravity_attempts",
    "antigravity_account",
    # cline retry/reroute trail from the previous run.
    "cline_attempts",
    "rerouted_to",
    "reroute_reason",
    "duration_seconds",
    "model_rationale",
    # Results of the previous run's sandbox. Left in place they point the
    # dashboard at a branch the next run has not produced.
    "sandbox_result",
    "repo_resolution",
)

if len(sys.argv) < 2:
    sys.exit("usage: requeue_task.py <task-id> [<task-id> ...]")

for task_id in sys.argv[1:]:
    matches = list(BASE.glob(f"*/{task_id}.json"))
    if not matches:
        print(f"  {task_id}: not found")
        continue
    src = matches[0]
    if src.parent.name == "pending":
        print(f"  {task_id}: already pending")
        continue
    data = json.loads(src.read_text())
    for field in RUN_FIELDS:
        data.pop(field, None)
    data["status"] = "pending"
    dest = BASE / "pending" / f"{task_id}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, dest)  # a runner scanning pending/ never sees half a file
    src.unlink()
    print(f"  {task_id}: {src.parent.name} -> pending ({data.get('assigned_to')})")
