# Swarm Failure & Improvement Analysis

**Date:** 2026-09-24 · **Scope:** `/home/setoo/YashDevops/Agentic_shared_memory` at commit `436bf45`
**Evidence:** 47 task records in `tasks/{active,queue,completed,failed}/`, 104,955 rows in
`runtime/logs/events.jsonl`, `config/providers.json`, live `brain health`

---

## 1. Headline

Nothing is currently stuck — `tasks/queue/` and `tasks/active/` are both empty, and all
9 agents report HEALTHY. The failures are historical, but they are **not random**: two
accounts fail 100% of the time for two specific, fixable configuration reasons, and one
observability defect is quietly consuming 22 MiB of the audit log.

| Outcome | Count | Share |
| --- | --: | --: |
| COMPLETED | 26 | 55.3% |
| FAILED | 10 | 21.3% |
| REJECTED (approval gate) | 5 | 10.6% |
| CANCELLED | 4 | 8.5% |
| VERIFICATION_COMPLETE | 2 | 4.3% |

The 5 REJECTED are **not failures** — they are destructive instructions ("Delete all build
artifacts and wipe the cache") correctly stopped at the approval gate. That subsystem works.

### Failure rate by agent

| Agent | OK | FAILED | Fail rate | Verdict |
| --- | --: | --: | --: | --- |
| `antigravity-account-2` | 10 | 2 | 17% | healthy |
| `antigravity-account-1` | 8 | 1 | 11% | healthy |
| `kiro-cli` | 7 | 2 | 22% | healthy |
| `antigravity-account-2077` | 0 | 2 | **100%** | broken — permissions |
| `antigravity-account-3` | 0 | 1 | **100%** | broken — permissions |
| `cline-account-2` | 0 | 2 | **100%** | broken — missing API key |

Three accounts have never completed a single task. They are counted as capacity by the
router and reported HEALTHY, so the swarm keeps assigning work to them.

---

## 2. Failure causes, in priority order

### F1 — Headless tool permissions auto-deny, killing every tool-using Antigravity task

**Severity: high. 3 of 10 failures. Affects `antigravity-account-2077`, `antigravity-account-3`.**

```
jetski: no output produced — a tool required the "read_file" permission that headless
mode cannot prompt for, so it was auto-denied. Add an allow-rule under
permissions.allow in settings.json (e.g. read_file(<target>)). Alternatively,
re-run with --dangerously-skip-permissions to auto-approve all tools.
```

Three separate permissions were hit across three tasks: `read_file`, `command`, `mcp`.
The pattern is consistent — the agent starts, runs 24–48 seconds, needs a tool, gets
auto-denied because headless mode has no human to prompt, and exits having produced
nothing.

The root cause is a stale assumption recorded in `config/providers.json`:

> `"dangerously_skip_permissions is false everywhere: headless execution was verified to
> succeed without it."`

That was verified against tasks needing **no tools**. Any task that reads a file, runs a
command, or calls an MCP server fails. Since "inspect the repository structure" is a
completely ordinary swarm task, this is not an edge case.

**Fix (least privilege, preferred):** add scoped allow-rules to each account profile's
`settings.json` — `read_file`, `list_directory`, and the specific commands the swarm
actually dispatches. Do **not** blanket-enable `--dangerously-skip-permissions`: these
agents run against real repositories.

**Also fix the note in `config/providers.json`.** It currently asserts something the
evidence contradicts, which is how this survived three failures.

### F2 — `cline-account-2` routes to a model it has no credential for

**Severity: high. 2 of 10 failures. 100% of that account's tasks.**

```
Google Generative AI API key is missing. Pass it using the 'apiKey' parameter
or the GOOGLE_GENERATIVE_AI_API_KEY environment variable.
```

Two compounding defects:

1. **No credential.** `GOOGLE_GENERATIVE_AI_API_KEY` is not set for that account.
2. **Config divergence — the more serious one.** The account's declared catalogue is
   `["anthropic/claude-fable-5.1", "deepseek/deepseek-v4-flash", "auto"]` with default
   `anthropic/claude-fable-5.1`. The model that actually ran was **`gemini-3.6-flash`**,
   which is not in that list at all.

The router resolved `auto` to a model outside the account's declared catalogue. That means
`config/providers.json` is not authoritative for what the account will execute, so model
declarations cannot be trusted for routing, cost attribution, or capability matching.

**Fix:** make `auto` resolve only within the declared catalogue and fail loudly otherwise;
then either set the credential or drop the Gemini path for this account.

### F3 — A failed remediation task inherited the same broken account

`task-e5721eb2` was auto-created to remediate `task-f4a4efa1`, was routed **back to
`cline-account-2`**, and failed with the identical missing-key error 58 seconds later.

The failover/remediation engine did not treat "this account has no credential" as a
property of the account. A credential failure is not transient, so retrying on the same
account cannot succeed — it only burns a task slot and doubles the noise.

**Fix:** classify failures. A missing-credential or permission-denied error should mark the
account unusable for that capability and route the retry elsewhere; only rate limits and
timeouts deserve a same-account retry. (This is the same distinction the failover engine
already makes for rate limits.)

### F4 — Three tasks recorded as FAILED that never executed

`task-a95be15d`, `task-f707ce76`, `task-807547ed` — all titled "Browser E2E Live Task" —
have `exit_code: null`, `duration: 0.0s`, and no response. They were dispatched and
abandoned, then written to `tasks/failed/`.

These inflate the failure rate with non-events. **Fix:** give them a distinct terminal
state (`ABANDONED` / `NOT_STARTED`) so the FAILED bucket means "ran and failed".

---

## 3. Systemic issues that are not task failures

### S1 — `AUTH_SUCCESS` floods the audit log: 71% of all events, 22.5 MiB

**Severity: high (data loss risk to the audit trail).**

| Event type | Rows | Share | Size |
| --- | --: | --: | --: |
| `AUTH_SUCCESS` | 74,662 | **71.1%** | **22.5 MiB** |
| `AGENT_HEALTH` | 8,126 | 7.7% | 4.7 MiB |
| `AUTH_FAILURE` | 2,386 | 2.3% | 0.7 MiB |
| everything else combined | ~19,800 | 18.9% | ~9 MiB |

One `AUTH_SUCCESS` row is written to the permanent event log for **every authenticated
dashboard poll**. Measured live: the file grew 55 KB in 5 seconds — about 9 events/second
while the dashboard merely sits open. Top paths:

```
19,883  /api/office/state
10,948  /api/tasks
 8,885  /api/accounts/metrics
 8,467  /api/agents
```

Consequences: the events file is 37 MB with **no rotation** (unlike
`routing_history.jsonl`, which has a `.1`), every consumer that replays it pays for 71%
noise, and the governance signal that actually matters — task lifecycle, routing,
failover — is buried at 1% density each.

**Fix:** stop journaling successful reads of loopback GET endpoints. Keep `AUTH_FAILURE`
(a real security signal), keep `AUTH_SUCCESS` for mutating endpoints (`/api/dispatch`,
`/api/execute`, `/api/worktrees/*`), and add size-based rotation to `events.jsonl` to match
the routing log.

### S2 — `brain health` cannot predict execution failure

All 9 agents report HEALTHY, including the three that fail 100% of tasks. Every reason
string is a binary-existence check:

```
kiro-cli        [HEALTHY]  Found ~/.local/bin/kiro-cli at /home/setoo/.local/bin/kiro-cli
cline-account-2 [HEALTHY]  Found ~/.local/bin/cline at /home/setoo/.local/bin/cline
```

"The binary exists" is not "this account can execute a task". Health does not check whether
a credential resolves or whether tool permissions are configured, which are exactly the two
things that broke. The router therefore treats broken accounts as available capacity.

**Fix:** extend health to verify the account's credential resolves and its profile has the
permissions the swarm needs, and surface a DEGRADED state for accounts that are installed
but unusable. A cheap, high-value proxy: mark an account DEGRADED after N consecutive
failures with a non-transient class.

### S3 — Model attribution is mostly absent

| `actual_model` | Records |
| --- | --: |
| `unknown` | 30 |
| `None` | 12 |
| real model id | 5 |

**89% of task records cannot say which model served the request.** 24 of 47 records also
report zero tokens. Cost attribution, per-model success rates, and the router's own
empirical feedback loop are all built on this field, so none of them can be trusted today.

This is partly honest design — `UNKNOWN_MODEL` is deliberately never replaced with the
*requested* model, which is correct — but the underlying gap is that most adapters do not
parse a model id or usage block out of their CLI's output. `antigravity-account-2`'s
`"gemini-3.8-flash-low (verified via CLI invocation)"` shows the pattern that works.

**Fix:** have each adapter parse its CLI's structured output for the served model and usage
where available, and record why attribution was unavailable when it is not.

### S4 — Error text is stored in the success field

For every failure, the error message is written to `result.response` — the field that holds
the model's answer on success:

```json
"result": {
  "response": "Google Generative AI API key is missing. Pass it using the 'apiKey' ...",
  "exit_code": 1,
  "status": "failed"
}
```

Any consumer reading `response` without first checking `status` will treat an error string
as legitimate content. That is how an error message ends up quoted into a downstream
handoff or a memory entry. The structured error does exist at
`result.execution_result.error.message`, so the fix is to stop double-writing it into
`response` and leave that field empty on failure.

---

## 4. What is working

Worth stating, because it narrows where effort belongs:

* **Approval gating** — 5 destructive instructions stopped before execution, zero leaked.
* **Account isolation** — 5 Antigravity accounts on separate profiles, no session bleed.
* **Exit-code handling** — three failures had `exit_code: 0` and were still correctly
  recorded as FAILED, so the adapters are not trusting the exit code alone.
* **Remediation dispatch** — auto-created a remediation task on failure. The mechanism
  fires correctly; only its target selection is wrong (F3).
* **Queue drain** — nothing stuck in `queue/` or `active/`; no orphaned in-flight work.

---

## 5. Recommended order of work

| # | Action | Fixes | Effort |
| --: | --- | --- | --- |
| 1 | Add scoped `permissions.allow` rules to the Antigravity account profiles; correct the false claim in `config/providers.json` | F1 | low |
| 2 | Stop journaling `AUTH_SUCCESS` for loopback GET reads; add rotation to `events.jsonl` | S1 | low |
| 3 | Constrain `auto` model resolution to the account's declared catalogue, failing loudly | F2 | low |
| 4 | Classify failures; never retry a credential/permission failure on the same account | F3 | medium |
| 5 | Deep health: verify credentials and permissions; DEGRADED after N non-transient failures | S2 | medium |
| 6 | Stop writing error text into `result.response` | S4 | low |
| 7 | Distinct terminal state for dispatched-but-never-executed tasks | F4 | low |
| 8 | Per-adapter model and usage parsing | S3 | medium |

Items 1–3 are configuration and a logging guard, and together they clear 5 of the 10
failures plus 71% of the audit-log volume.

---

## 6. Caveats on this analysis

* 47 task records is a small sample; the per-agent fail rates for the three 100%-failure
  accounts rest on 1–2 tasks each. The *causes* are unambiguous from their error text, but
  the rates are not statistically meaningful.
* Task records carry no `state` field; status lives in `status`. Any tooling written against
  `state` will silently read `None` — I made that mistake myself while investigating.
* The failures span 2026-09-08 to 2026-09-24, so some may already be fixed by intervening
  changes. F1 and F2 were confirmed against the *current* `config/providers.json`.
* I did not re-run any failed task. Re-running `task-7c415b72` after adding permission
  rules would confirm F1's fix; that is the cheapest verification available.
