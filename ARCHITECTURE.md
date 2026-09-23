# Architecture, Storage & Setup

How Agentic Brain fits together, where your data lives, and how to run it on your own
machine. For the feature tour see [README.md](README.md); this document covers the
parts you need to operate or contribute to the system.

- [1. End-to-end architecture](#1-end-to-end-architecture)
- [2. Shared memory and history: storage and sync](#2-shared-memory-and-history-storage-and-sync)
- [3. Setup on your own machine](#3-setup-on-your-own-machine)
- [4. MCP servers used, and why](#4-mcp-servers-used-and-why)
- [5. What is local to you and never committed](#5-what-is-local-to-you-and-never-committed)

---

## 1. End-to-end architecture

A single mission flows through six stages. Each is a separate module, so a stage can be
read, tested, or replaced on its own.

```
  You (Mission Control UI or CLI)
        │
        │  POST /api/dispatch      ← job submission
        ▼
  ┌───────────────┐   classify      ┌──────────────────┐
  │ TaskManager   │ ───────────────▶│ TaskClassifier   │  brain/router/classification.py
  │ tasks/        │                 │ category,        │
  └───────┬───────┘                 │ complexity, risk │
          │                         └────────┬─────────┘
          │                                  │
          │                         ┌─────────▼────────┐
          │                         │ SmartRouter      │  brain/router/smart_router.py
          │                         │ scores agents,   │
          │                         │ picks model tier │
          │                         └─────────┬────────┘
          ▼                                   │
  ┌───────────────────────────────────────────▼──────────┐
  │ Agent adapters  (agents/<agent>/adapter.py)          │
  │   antigravity · kiro · cline · openhands             │
  │   each account = its own isolated profile directory   │
  └───────┬──────────────────────────────────────────────┘
          │  runs the agent CLI headlessly in a git worktree
          ▼
  ┌──────────────────┐   writes    ┌─────────────────────┐
  │ Worktree sandbox │ ──────────▶ │ MemoryStore (SQLite)│  memory/store/
  │ runtime/sandboxes│             │ + EventBus (JSONL)  │  events/bus.py
  └──────────────────┘             └──────────┬──────────┘
                                              │ read back on the next task
                                              ▼
                                       HandoffManager  handoffs/
```

**Routing is the interesting part.** `TaskClassifier` turns free text into three
signals, and `select_model()` maps them onto a model tier per agent:

| Signal | Source | Effect |
| --- | --- | --- |
| `category` | keyword rules | which capabilities an agent must have |
| `suggested_complexity` | keyword rules | target model strength (`fast` → `reasoning`) |
| `risk` | `infer_risk()` | escalates to the strongest tier on `high`/`critical` |

Complexity and risk are deliberately **independent axes**. A one-line command can be
catastrophic ("drop the production database") while a long refactor is harmless, so
blast radius is derived separately from difficulty. Risk only escalates when the task
actually performs an operation — describing one ("document the production checklist")
does not.

**Isolation.** Every account runs under its own profile directory, passed as
`--app_data_dir`. Antigravity additionally shields the OS keyring per process, because
otherwise every profile authenticates as whichever Google account signed in last and
the "account pool" is one account wearing several hats. Every agent runs inside a git
worktree under `runtime/sandboxes/`, so a failed run never touches your checkout.

---

## 2. Shared memory and history: storage and sync

### Where it lives

One SQLite database on **your** machine:

```
memory/store/shared_memory.db        tables: memories, memory_retrievals
```

Created automatically on first use by `MemoryStore._init_db()`. A fresh clone needs no
migration, no server, and no configuration.

### Why SQLite

This project has **zero third-party dependencies**, and `sqlite3` ships with Python. It
also solves the problem that actually matters here: the dashboard is threaded *and* the
agent CLIs are separate processes, so several writers touch the same store at once.
SQLite's locking is inter-process, so they can. The alternatives were rejected for
concrete reasons — JSONL loses the query layer and needs its own locking; IndexedDB
lives in one browser profile and is invisible to the Python backend and the CLI agents,
so it can only ever be a cache, not the store; and any embedded store from PyPI breaks
the works-on-a-fresh-clone property for no benefit at single-user scale.

### How writes stay durable

Every connection sets three pragmas (`memory/store/memory_store.py`):

```python
PRAGMA journal_mode=WAL      # readers proceed during a write
PRAGMA busy_timeout=5000     # a writer collision waits instead of failing
PRAGMA synchronous=NORMAL    # crash-safe under WAL
```

This is not cosmetic. Without them, 200 concurrent writes lost **112 entries** to
`database is locked`, because nothing retried and the caller never saw the error. With
them, the same test loses none. Retrieval-history failures are now logged rather than
swallowed, so a dropped row is visible instead of silent.

### How the dashboard stays in sync

There is no sync protocol. The dashboard and the agents read and write **the same
file**, so they cannot disagree. The UI stays current by polling:

- `refreshData()` runs every 3s and refreshes status, accounts, tasks and agents.
- `refreshActiveSection()` additionally reloads whatever section you are looking at —
  Shared Memory, Accounts, Providers or Events.

The second function exists because those loaders previously ran **only on tab switch**.
A memory written by an agent did not appear until you navigated away and back, and a
newly registered account stayed invisible. Scoping the refresh to the visible tab keeps
it live without polling every endpoint in the application every three seconds.

The Shared Memory agent filter is populated from `/api/agents` on each refresh. It used
to be a hardcoded list of four agents, so any account added later could never be
selected no matter how far you scrolled.

---

## 3. Setup on your own machine

### Requirements

- **Python 3.10+** — standard library only, no `pip install` step for the core.
- **Git 2.30+** — worktree sandboxing depends on it.
- Linux or macOS.
- Optional, only for the agents you actually want to run: the `agy` (Antigravity),
  `cline`, or `kiro-cli` binaries. The system starts and the dashboard works with none
  of them installed; those agents simply report as unavailable.

### Run it

```bash
git clone https://github.com/YashJadhav1023/Agentic-Brain.git
cd Agentic-Brain
cp .env.example .env

# Verify the install: standard library only, no dependencies to fetch.
python3 -m unittest discover -s tests/unit -t .

# Optional: hermetic end-to-end smoke test (CLI, every dashboard route, memory,
# tasks, handoffs). Uses a temp copy and shimmed agent CLIs; never runs a real agent.
python3 scripts/smoke_test.py

# Start Mission Control on http://127.0.0.1:3333
python3 ui/dashboard/dashboard.py
```

On first start the dashboard generates `runtime/mission_control.token` (mode `0600`).
The UI fetches it from the unauthenticated, loopback-only `GET /api/token` endpoint and
then sends `Authorization: Bearer <token>` on every API call (the SSE stream
`/api/events/stream` takes it as `?token=` because `EventSource` cannot set headers).
Protected endpoints return 401 without it, for example:

```bash
TOKEN=$(curl -s http://127.0.0.1:3333/api/token | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:3333/api/tasks
```

### Configuration

Everything is optional — the defaults work on a clean clone.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_PORT` | `3333` | Dashboard port |
| `BRAIN_HOST` | `127.0.0.1` | Dashboard bind address; only loopback addresses are accepted |
| `BRAIN_PROVIDERS_CONFIG` | `config/providers.json` | Alternate providers/accounts file (tests and sandboxes use a throwaway copy) |
| `MISSION_CONTROL_AUTH_TOKEN` | *(generated)* | Use this bearer token instead of `runtime/mission_control.token` |
| `BRAIN_DIR` | `~/agentic-brain` | Shared-brain store root on this machine |
| `BRAIN_WORKSPACE_ROOT` | current directory | Where to discover docs, skills and steering files |
| `BRAIN_REPO_ROOT` | current directory | Where to discover sibling git repositories |
| `BRAIN_PROTECTED_PIDS` | *(empty)* | Comma-separated PIDs the dashboard must never signal |
| `MAX_CONCURRENT_AGENTS` | `2` | Parallel agent cap |

`BRAIN_PROTECTED_PIDS` is empty by default on purpose. It used to be a hardcoded PID
guarding one developer's IDE, which is meaningless on your machine and actively unsafe
once the operating system recycles that number onto a different process.

### Network exposure

The dashboard binds `127.0.0.1` only and rejects non-loopback binds. Do not put it on a
public interface: it can dispatch agent jobs and execute git operations. If you need
remote access, front it with a reverse proxy that adds its own authentication.

---

## 4. MCP servers used, and why

Two MCP servers participate in this workflow. Both are optional; the orchestrator runs
without them.

| Server | Package | Why it is used |
| --- | --- | --- |
| **`brain`** | [`basic-memory`](https://github.com/basicmachines-co/basic-memory) | The cross-agent memory and handoff store. Gives every agent one persistent Markdown knowledge graph (default `~/agentic-brain`) so that when one agent exhausts its context budget, another resumes the same task from the same state instead of starting over. This is what makes the handoff protocol work across different vendors' agents. |
| **`firecrawl`** | [`firecrawl-mcp`](https://github.com/firecrawl/firecrawl-mcp-server) | Web search and page extraction. Used when a routing or design decision needs current external information that is not in the codebase or the model's training data — for example comparing model-routing strategies before changing `select_model()`. Chosen over a bare search API because it returns extracted page content, not just links. |

**A note on names.** Only these two are wired into this workflow. There is no
*Sequential Thinking MCP* and no *Filesystem MCP* here — filesystem access is native to
the agents rather than mediated by an MCP server, and task decomposition is handled by
`brain/router/` in this repository, not by an external server.

Separately, `brain resources` / `brain tools` can *discover and catalogue* MCP servers
installed on your machine for inventory purposes. Discovery is not the same as use: a
catalogued server is not part of this workflow unless it appears in the table above.

---

## 5. What is local to you and never committed

Runtime state stays on your machine and is excluded by `.gitignore`. Cloning the
repository gives you the code, not someone else's history.

| Path | Contents |
| --- | --- |
| `memory/store/shared_memory.db` | Your shared memory and retrieval history |
| `runtime/mission_control.token` | Your dashboard bearer token (`0600`) |
| `runtime/logs/`, `runtime/analytics/` | Your event ledger and telemetry |
| `runtime/sandboxes/` | Git worktrees for agent runs |
| `tasks/{queue,active,completed,failed}/*.json` | Your task records |
| `sessions/*.json` | Your session registry |
| `handoffs/current.md`, `handoffs/current.json` | Your live handoff baton |
| `handoffs/archive/` | Your archived handoffs |
| `ui/dashboard/runtime/` | Audit log written when the dashboard is started from `ui/dashboard/` |
| `creds_oauth.json`, `.env` | Your credentials — never tracked |

Credentials are read at runtime from your own environment or from files under `~`, and
are never written into the repository. `config/providers.json` is a **template** that
ships with placeholder addresses; replace the account entries with your own.
