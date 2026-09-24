# Shared Brain: Cross-Agent Memory and Handoff Protocol

Every AI agent on this machine — Kiro CLI, Kiro VS Code, Amazon Q CLI, Amazon Q
VS Code, Antigravity IDE, Antigravity CLI, and Cline — is connected to
**one** persistent memory store through an MCP server named `brain`.

The brain is a local knowledge graph of Markdown files at `~/agentic-brain`.
There is no cloud, no account, and no second LLM. Whatever one agent writes,
every other agent reads.

**Why this exists:** when one agent runs out of context or token budget, you
switch to another agent and it continues the same task from the same state
instead of starting over.

---

## The three rules

### 1. Read the brain before you do anything

At the start of every session, before your first substantive action:

1. `recent_activity(timeframe="7d")` — what has been happening.
2. `read_note("handoff/current")` — the live baton: the exact task in flight.

Then state in one line what you resumed, for example:

> Resumed from brain: "Wire brain MCP into 4 agents", step 4 of 6, last touched
> by kiro-cli.

If `handoff/current` does not exist, say the brain is empty and proceed fresh.
Do not silently skip this step and do not ask permission to read the brain.

### 2. Write a checkpoint before you run out

Write or overwrite `handoff/current` **immediately** when any of these happen:

- Your context or token budget is running low, or compaction is approaching.
- The user says "checkpoint", "switch agent", "handoff", or "save context".
- You finish a task or a meaningful milestone.
- You hit a blocker you cannot resolve.

Use `write_note` with `directory="handoff"`, `title="current"`,
`note_type="handoff"`, `overwrite=true` and exactly this structure. Fill every
field — a checkpoint missing `[next]` is useless to the agent that picks it up.

```markdown
---
title: current
type: handoff
tags: [handoff, active]
---

# <Task in one line>

## Observations
- [status] in-progress | blocked | done
- [agent] which agent wrote this (kiro-cli, amazon-q, antigravity, antigravity-ide, cline)
- [scope] repo path and, if relevant, the DEV/UAT/PROD environment
- [done] what is actually finished and verified, one bullet per item
- [next] the single next action, concrete enough to execute without guessing
- [file] each file created or modified, with why
- [command] exact commands to re-run to verify current state
- [blocker] anything unresolved, or "none"
- [decision] choices made that the next agent must not silently reverse

## Relations
- continues [[handoff/<previous checkpoint>]]
- relates_to [[projects/<project>]]
```

Also append progress to the same note as you go, using `edit_note` in append
mode, rather than waiting for the end of the session. A crash should never lose
more than one step.

#### This structure is enforced, not advisory

`schemas/handoff.md` in the brain is a Picoschema note with
`settings.validation: strict`. Every note with `type: handoff` is validated
against it, so a checkpoint that omits `[next]` is a hard error, not a warning:

```
- error: Missing required field: next (expected [next] observation)
```

| Observation | Rule |
| --- | --- |
| `[status]` | required, exactly one, and exactly `in-progress`, `blocked`, or `done` — no trailing prose |
| `[agent]` `[scope]` `[next]` `[blocker]` | required, exactly one each, non-empty |
| `[done]` | required, one or more bullets |
| `[file]` `[command]` `[decision]` | optional, repeatable |

Check your own checkpoint before you hand over:

```bash
brain validate                              # whole store, incl. cardinality
bm schema validate --type handoff           # schema-only view
```

Picoschema checks presence and enum membership but not cardinality, so duplicate
`[status]` lines pass `bm schema validate` and are rejected by `brain validate`.
Run `brain validate` — it is the stricter of the two.

Put commentary in its own category rather than on `[status]`. Write
`- [archived] 2026-08-27; superseded by ...`, never
`- [status] done — archived 2026-08-27`.

### 3. Archive, then reset

When a task is finished, `move_note` the checkpoint from `handoff/current` to
`handoff/<YYYY-MM-DD>-<slug>`, and record anything durable as its own note:

- `decisions/<slug>` — an architectural or tooling decision and its rationale.
- `projects/<name>` — what a project is, how to run it, how it is deployed.
- `runbooks/<slug>` — a procedure that worked and should be repeated.

Durable knowledge belongs in `decisions/`, `projects/`, and `runbooks/`.
`handoff/current` is only ever the in-flight baton.

---

## Brain layout

```
~/agentic-brain/
  handoff/current.md              the live baton — read first, write last
  handoff/2026-08-27-<slug>.md    archived checkpoints
  projects/<name>.md              per-project overview and run instructions
  decisions/<slug>.md             decisions with rationale
  runbooks/<slug>.md              procedures that worked
  schemas/handoff.md              the enforced handoff contract (strict)
```

## Tools available on the `brain` server

| Purpose | Tool |
| --- | --- |
| Resume context | `read_note`, `recent_activity`, `build_context` |
| Search by meaning | `search_notes`, `search` |
| Save context | `write_note`, `edit_note` |
| Browse | `list_directory`, `view_note`, `read_content` |
| Archive / clean up | `move_note`, `delete_note` (needs confirmation) |

Read and write tools are pre-approved so a handoff never stalls on a prompt.
`move_note`, `delete_note`, and `delete_project` still require confirmation.

## Exact call signatures

These parameter names are non-obvious — use them verbatim. `write_note` takes
`directory`, not `folder`, and `note_type`, not `entity_type`.

```
recent_activity(timeframe="7d")
read_note(identifier="handoff/current")
search_notes(query="<text>")
write_note(title="current", directory="handoff", note_type="handoff",
           content="<full markdown>", overwrite=true)
edit_note(identifier="handoff/current", operation="append", content="<text>")
move_note(identifier="handoff/current", destination_path="handoff/<date>-<slug>.md")
```

## Autonomous Multi-Agent Swarm & Live Task Distribution

When the user says **"use brain"**, **"use multi agent"**, **"distribute tasks"**, or presents multiple tasks at once:
1. **Smartly distribute tasks across agents** based on their core specializations rather than running everything in a single context:
   - **`kiro-cli`** (Terminal & Cloud Workhorse): Terminal execution, shell scripts, Docker builds, Azure CLI (`az`), Kubernetes (`kubectl`), npm/pip batch jobs, automated tests.
   - **`cline`** (Headless Focused-Coding CLI): Focused code work contained to one or a few files — implementing functions, fixing bugs, adding tests and docstrings, frontend styling in React/Tailwind/CSS.
   - **`antigravity`** (Master Architect, Reasoning & Multi-File Synthesis): Architecture planning, deep reasoning, protocol design, governance, escalation resolution, **and work that spans many files** — refactors across a codebase, renames everywhere, restructuring, splitting modules. This is the default for large code work because it runs headlessly.
   - **`antigravity-ide`** (Antigravity IDE, In-Editor): **Opt-in only.** Ask for it by name, or with `--agent antigravity-ide`. It cannot be run headlessly, so the swarm only *delivers* to it and a human must drive its Agent panel. It never wins a task by keyword, because a task routed here does not execute until someone works it by hand.
2. **Every headless task runs in a guarded git sandbox.** Headless agents run with
   their approval gates disabled, so they are never pointed at your checkout. Each
   task gets a `git worktree` on a throwaway branch `brain/swarm/<task-id>`, and
   whatever the agent changes is committed to that branch. Your working tree and
   current branch are never modified. Review or discard afterwards:

```bash
git -C <repo> diff <base>..brain/swarm/<task-id>   # review what the agent did
git -C <repo> branch -D brain/swarm/<task-id>      # discard it entirely
```

   The sandbox is scoped to exactly one repository, because the workspace root is
   a polyrepo and not a git repository itself. The target repo is taken from
   `--repo`, or inferred when the task names one. A task that spans several
   repositories (`across every service`) is **escalated, not half-executed**;
   re-dispatch it once per repository. A task that names no repository runs with
   no project checkout at all. Set `BRAIN_SWARM_DEFAULT_REPO` to pick a default,
   or `BRAIN_SWARM_SANDBOX=0` to disable the guard.
3. **Antigravity is two workers, not one.** Each account is separate capacity with
   its own quota, and both run **at the same time**: a batch of Antigravity-class
   tasks is alternated across whichever accounts are attached, and `swarm run`
   executes them in parallel.

   - **`antigravity`** — your signed-in Google account, token in the OS keyring,
     **all models** (Gemini, Claude, GPT). Data dir `~/.gemini/antigravity-cli`.
   - **`antigravity-api`** — a second account authenticated by a Gemini API key.
     Requests go **straight to the Gemini API**, so **Gemini models only**, and
     usage bills to the key rather than an Antigravity seat. Data dir
     `~/.gemini/antigravity-api`, needs both `"modelProvider": "gemini"` in that
     dir's `settings.json` and `GEMINI_API_KEY` in the environment — the key alone
     does nothing.

   Each prefers its own account and falls back to the other, so a quota wall on one
   does not stall the queue:

   Attach the second account once, with the key entered at a silent prompt so it
   never reaches shell history or a log:

```bash
scripts/brain/attach-antigravity-key.sh   # writes a 0600 key file
brain swarm status                        # shows READY / NOT ATTACHED per account
```

   Accounts are isolated by `--app_data_dir`, so adding one can never disturb the
   other's session. A quota, rate-limit, 429 or RESOURCE_EXHAUSTED error, or
   Antigravity's opaque `Agent execution terminated due to error`, **fails the task
   over to the other account**; a diagnosable task error stops immediately instead
   of burning the pool. The account that served a task and the full attempt trail
   are recorded on it. Set `BRAIN_ANTIGRAVITY_MULTI_ACCOUNT=0` to use only the
   signed-in account.

   Only Gemini ids **verified to answer on an API key** are offered:
   `gemini-3.8-flash-medium`, `gemini-3.7-flash-high`, `gemini-3.7-flash-medium`,
   `gemini-3.8-flash-low`, `gemini-3.7-flash-low`. Pro tiers and
   `gemini-3.8-flash-high` were probed and rejected.

4. **Dispatch and monitor tasks using the Swarm CLI:**

```bash
# 1. Dispatch a batch of tasks (smart routed automatically)
brain swarm dispatch "Run git status; Refactor modal styling in Tailwind; Design database schema"

# 2. Dispatch and immediately execute in parallel across agents
brain swarm dispatch "Run kubectl get pods -n agentic-os; Design microservice API" --run

# 3. Target a specific repository for the sandbox
brain swarm dispatch "Split the auth guards into separate modules" --repo agentic-os-mcp --run

# 4. View live queue and agent statuses
brain swarm status

# 5. Open the visual Swarm Radar UI
brain ui
```

5. **Three agents execute headlessly; one is delivered to.** `kiro-cli`,
   `antigravity` and `cline` each expose a real CLI, so the swarm runs them and
   selects their model per task automatically on capability fit. `antigravity-ide`
   is an editor launcher with no prompt mode, so the swarm records a durable
   *delivery* for it, which is never evidence of execution. Its model cannot be
   set from outside either, so each delivery carries a **recommended capability
   tier and ranked candidate models**. If a task was delivered to you, switch to
   the recommended model, then record each step yourself:

```bash
brain swarm antigravity-ide status                          # what was delivered
brain swarm antigravity-ide acknowledge task-xxxxxxxx --model <model-you-used>
brain swarm antigravity-ide progress task-xxxxxxxx -n "..." # evidence of work
brain swarm antigravity-ide complete task-xxxxxxxx -n "..." # verified completion
brain swarm antigravity-ide escalate task-xxxxxxxx -n "..." # blocked; escalate
```

   Read the recommended model from `swarm/antigravity-ide/CURRENT_TASK.md` or the
   delivery record. Reporting `--model` is what makes the choice verifiable; the
   brain records exactly what you report and never infers it. Transitions are
   enforced: progress before acknowledgement is rejected, so never claim a state
   you did not reach.

   For the delivery agent, availability has two independent parts. `ready` means
   it is **installed**, proven by a real marker such as the `antigravity-ide`
   binary. It never means the agent did anything. Only a recorded acknowledgement
   moves it past `ready`.

   Acknowledging a delivery is **receipt, not execution**. Nothing starts running
   because a delivery was acknowledged, and no notification may ever record an
   acknowledgement on a human's behalf — see
   `decisions/desktop-notifications-must-never-record-approval`.

6. **Inter-Agent Escalation**: If a worker agent hits a blocker, it moves the task to `escalated/`; Antigravity analyzes the blocker, applies the fix or records the architectural decision in `decisions/`, and unblocks the worker.

## Never put these in the brain

The brain is plain Markdown on disk and is read by every agent. Do not write:

- API keys, tokens, passwords, connection strings, or kubeconfig contents.
- Raw secret values pulled from Key Vault, environment variables, or logs.
- Full log dumps. Record the finding, not the payload.

Refer to secrets by name only — `CLOUDFLARE_API_TOKEN`, not its value. This
extends the existing rule in `.kiro/steering/mcp-usage.md`.

## Environment safety

When a checkpoint concerns Azure infrastructure, record the environment
explicitly (`[scope] PROD`) using the mappings in
`.kiro/steering/azure-env-map.md`. An agent resuming work must never infer the
environment. If the scope is missing, ask before touching infrastructure.

## Helper CLI

`scripts/brain/brain` wraps the same store for use outside an agent session:

```bash
brain status                  # is the brain healthy, what is in flight
brain resume                  # handoff briefing: age, last agent, done/stale warnings, then the baton
brain checkpoint "<task>"     # write/open a checkpoint (interactive or headless);
                              # validated + secret-redacted before an atomic write,
                              # an invalid baton is refused and never saved
brain log                     # recent activity across all agents
brain find "<query>"          # search the brain
brain archive <slug>          # archive current handoff to <date>-<slug> (never overwrites)
brain archive --auto          # same, slug derived from the baton's H1
brain validate [--fix]        # validate store markdown, schema and secret leaks;
                              # --fix splits prose off [status] and redacts secrets
brain heal [--dry-run]        # autonomous self-healing (schema, links, db, sidecar)
brain expand <query>          # 2-hop graph-augmented cognitive context expansion
brain watch                   # continuous autonomous sentinel monitor
brain swarm dispatch "<tasks>"# smart route and queue batch tasks
brain swarm status            # check multi-agent swarm queue & worker states
brain swarm run               # execute pending swarm tasks in parallel
brain swarm cline <event>     # record Cline delivery lifecycle evidence
brain swarm antigravity-ide <event>  # record Antigravity IDE delivery lifecycle evidence
brain ui                      # launch visual Knowledge Graph & Swarm Radar
```
