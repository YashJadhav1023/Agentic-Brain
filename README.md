# Agentic Shared Memory & Multi-Agent Swarm Orchestrator

[![Multi-Agent Status](https://img.shields.io/badge/Agents-4%20Active%20Headless-emerald.svg)](./docs/AGENTS.md)
[![Hardware](https://img.shields.io/badge/Host-CachyOS%20Linux%20(2%20cores%20/%2016GB)-blue.svg)](./ARCHITECTURE.md)
[![Security](https://img.shields.io/badge/Security-Zero%20Secret%20Leak-success.svg)](./docs/SECURITY.md)
[![Tests](https://img.shields.io/badge/Tests-unittest%20%2B%20smoke-brightgreen.svg)](#20-development-benchmarks--testing)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](./LICENSE)

**Agentic Brain** is an enterprise-grade, ultra-lightweight autonomous multi-agent development and orchestration platform. It operates as a unified cognitive coordination layer over independent coding agents (**Google Antigravity Account 1**, **Google Antigravity Account 2**, **Kiro CLI**, and **Cline CLI**), providing autonomous multi-factor routing, contextual BM25 shared memory, structured handoffs, universal cross-agent continuation, automated rate-limit failover, git worktree sandboxing, and a real-time reactive Mission Control operations cockpit.

---

> **New here?** **[ARCHITECTURE.md](ARCHITECTURE.md)** is the place to start: the
> end-to-end request flow, how shared memory is stored and kept in sync, setup on
> your own machine, and which MCP servers are used and why.

## Table of Contents

1. [What Agentic Brain Is](#1-what-agentic-brain-is)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Supported Agents](#3-supported-agents)
4. [Antigravity Account 1](#4-antigravity-account-1)
5. [Antigravity Account 2 & Headless Architecture](#5-antigravity-account-2--headless-architecture)
6. [Kiro CLI](#6-kiro-cli)
7. [Cline CLI](#7-cline-cli)
8. [Targeted Shared Memory](#8-targeted-shared-memory)
9. [Multi-Factor Smart Routing](#9-multi-factor-smart-routing)
10. [Autonomous Failover Engine](#10-autonomous-failover-engine)
11. [Universal Continuation & Verification Contract](#11-universal-continuation--verification-contract)
12. [Git Worktree Sandboxing](#12-git-worktree-sandboxing)
13. [Mission Control Operations Cockpit](#13-mission-control-operations-cockpit)
14. [Hardware Requirements & Concurrency Limits](#14-hardware-requirements--concurrency-limits)
15. [Security & Isolation Model](#15-security--isolation-model)
16. [Installation & Prerequisites](#16-installation--prerequisites)
17. [Configuration & Portability](#17-configuration--portability)
18. [Running the Brain CLI](#18-running-the-brain-cli)
19. [Running Mission Control](#19-running-mission-control)
20. [Development, Benchmarks & Testing](#20-development-benchmarks--testing)

---

## 1. What Agentic Brain Is

Traditional multi-agent setups suffer from context thrashing, lack of shared working state, duplicate executions, and manual copy-pasting across browser tabs and IDE windows. **Agentic Brain** solves this by acting as an autonomous orchestrator that treats disparate agent CLIs as specialized cognitive workers:

```
                          USER / MISSION CONTROL UI
                                      │
                                      ▼
                                AGENTIC BRAIN
                                      │
      ┌──────────────────┬────────────┴───────────┬──────────────────┐
      ▼                  ▼                        ▼                  ▼
ANTIGRAVITY ACCT 1  ANTIGRAVITY ACCT 2        KIRO CLI           CLINE CLI
(Architecture/Gov)  (Refactor/Headless)      (Terminal/Test)     (UI/Styling)
      │                  │                        │                  │
      └──────────────────┴────────────┬───────────┴──────────────────┘
                                      ▼
                        SHARED STATE & MEMORY LAYER
                   ┌──────────────────────────────────────┐
                   │ • SQLite FTS5 BM25 Memory Retrieval   │
                   │ • Context Optimizer (>75% Reduction) │
                   │ • Git Worktree Sandboxes             │
                   │ • Structured Handoff Records         │
                   │ • Universal Continuation Engine      │
                   │ • Live Token & Stage Telemetry       │
                   └──────────────────────────────────────┘
```

---

## 2. High-Level Architecture

The Brain consists of modular, loosely coupled subsystems built strictly on Python 3 standard library modules:

* **Task Lifecycle Engine (`tasks/`):** Strict state tracking across stages:
  `QUEUED` → `ROUTING` → `MEMORY` → `EXECUTION` → `HANDOFF` → `CONTINUATION` → `VERIFICATION` → `COMPLETE` (or terminal failure states `FAILED`, `CANCELLED`, `BUDGET_EXHAUSTED`, `DEPTH_LIMIT_REACHED`).
* **Provider Registry (`providers/`):** Dynamic registration and discovery of heterogeneous agent providers and execution accounts with declarative capability and model catalogs.
* **Smart Router (`brain/router/`):** 8-factor multi-factor scoring algorithm selecting the optimal agent and model tier based on keywords, declared capabilities, model strength, historical success rate, current load, and latency.
* **Context Optimizer (`brain/context/`):** Reduces bloated user prompts and project guidelines by >75% to eliminate context pollution while preserving key architectural constraints.
* **Event Telemetry Bus (`events/`):** Append-only structured JSONL event bus for real-time observability and auditability.
* **Worktree Manager (`brain/worktree/`):** Creates temporary git worktrees (`git worktree add`) for each task, allowing review and approval of code changes before merging to `main`.

---

## 3. Supported Agents

| Agent Resource | Provider | Primary Role | Capabilities | Execution Mode |
| :--- | :--- | :--- | :--- | :--- |
| **`antigravity-account-1`** | Google Antigravity | Master Architect & Deep Reasoning | `architecture`, `deep-reasoning`, `governance`, `protocol-design`, `documentation` | Headless CLI (`agy`) |
| **`antigravity-account-2`** | Google Antigravity | Headless Refactoring & Code Review | `architecture`, `code-review`, `component-refactoring`, `editor-refactoring`, `test-scaffolding` | Headless CLI (`agy`) |
| **`kiro-cli`** | Kiro | Terminal Operations & Validation | `build-and-test`, `local-validation`, `terminal-operations`, `cloud-read-only`, `kubernetes-read-only` | Headless CLI (`kiro-cli`) |
| **`claude-account-N`** | Claude Code | Deep Reasoning, Review & Refactoring | `deep-reasoning`, `architecture`, `code-review`, `component-refactoring`, `editor-refactoring`, `test-scaffolding`, `documentation` | Headless CLI (`claude -p`) |
| **`cline`** | Cline | UI Styling & Component Polish | `code-review`, `component-refactoring`, `editor-refactoring`, `frontend-styling` | Headless CLI (`cline`) |

---

## 4. Antigravity Account 1

`antigravity-account-1` is configured with the primary Google Antigravity profile (`~/.gemini/antigravity-cli`). It is routed high-complexity architectural design tasks, governance decisions, and deep protocol specifications using high-tier models (e.g. `claude-opus-4-6-thinking`, `gemini-3.1-pro-high`).

---

## 5. Antigravity Account 2 & Headless Architecture

A critical architectural milestone of Agentic Brain: **Antigravity Account 2 runs 100% headlessly without opening the Antigravity IDE GUI.**

```
Brain Orchestrator
       │
       ▼
AntigravityAccountAdapter
       │
       ▼
agy --app_data_dir=antigravity-ide --output-format json -p "<prompt>"
       │
       ▼
Isolated Account 2 Profile (~/.gemini/antigravity-ide)
       │
       ▼
Response JSON Parsing (conversation_id, duration_seconds, output)
       │
       ▼
Handoff Generation & Mission Control Telemetry
```

* **No GUI Automation:** Does not require X11/Wayland window capture, key presses, or IDE launches.
* **Complete Profile Isolation:** Data directory parameter `--app_data_dir=antigravity-ide` prevents token, auth, or session bleed with Account 1.
* **Least-Privilege Execution:** Tool executions adhere to profile settings without `--dangerously-skip-permissions` unless explicitly enabled.

---

## 6. Kiro CLI

`kiro-cli` provides headless terminal operations, shell execution, build-and-test runs, and read-only environment inspections. When a task requires running integration tests or inspecting cloud resources, the Smart Router automatically assigns it to `kiro-cli`.

---

## 7. Cline CLI

`cline` provides headless code styling, CSS adjustments, component polish, and frontend layout modifications. It is selected for tasks with `frontend-styling` and component refactoring affinity.

---

## 8. Targeted Shared Memory

The memory subsystem (`memory/store/memory_store.py`) uses SQLite with full-text search (FTS5) and BM25 ranking:

* **Scope Isolation:** Memories are partitioned across scopes (`GLOBAL`, `PROJECT`, `AGENT`, `TASK`).
* **Relevance Filtering:** Queries extract only the top relevant memories based on BM25 rank, excluding irrelevant context noise.
* **Context Optimization:** Prompts are stripped of repetitive boilerplate, yielding >75% character reduction while retaining essential constraints.
* **Zero Bloat:** Runs embedded in SQLite without requiring external vector database daemons (Chroma, Pinecone, etc.).

---

## 9. Multi-Factor Smart Routing

The Smart Router (`brain/router/smart_router.py`) evaluates all active agents through an 8-factor scoring equation:

$$\text{Score} = w_{\text{kw}} \cdot S_{\text{kw}} + w_{\text{cap}} \cdot S_{\text{cap}} + w_{\text{model}} \cdot S_{\text{model}} + w_{\text{health}} \cdot S_{\text{health}} + w_{\text{success}} \cdot S_{\text{success}} - w_{\text{load}} \cdot S_{\text{load}} - w_{\text{lat}} \cdot S_{\text{lat}} + w_{\text{pref}} \cdot S_{\text{pref}}$$

Every decision logs candidate scores and an explainable rationale to `runtime/logs/routing_history.jsonl`, visible live in Mission Control.

---

## 10. Autonomous Failover Engine

When a provider encounters a transient or rate-limit error (e.g. `429 Quota Exceeded`, timeout, socket hangup):

1. **Error Interception:** `FailoverManager` classifies the failure (`rate_limit`, `timeout`, `crash`).
2. **Identity Preservation:** The failing task is NOT duplicated; its original `task_id` is preserved.
3. **Automatic Fallback:** The secondary candidate (e.g. AG-1 → AG-2 or AG-2 → AG-1) is immediately assigned.
4. **Telemetry & Auditing:** Failover events (`PROVIDER_FAILOVER`, `TASK_FAILOVER`) are emitted and visualized on the task card in Mission Control.

---

## 11. Universal Continuation & Verification Contract

When an agent completes an initial task stage, it writes a structured Picoschema handoff (`handoffs/current.json`). The Universal Continuation Engine (`brain/context/continuator.py`) allows any agent to resume:

* **Continuation Depth Limit:** Default maximum 5 continuations to prevent runaways.
* **Single-Depth Verification:** Verification tasks have a strict depth limit of 1 (`verification_depth=1`). Once verified, the task reaches terminal state `VERIFICATION_COMPLETE`.
* **Idempotent Termination:** Prevents infinite ping-pong loops between verification agents.

---

## 12. Git Worktree Sandboxing

To guarantee codebase safety:

* Every modifying task can execute inside an isolated Git worktree (`runtime/sandboxes/<task_id>`).
* Code modifications stay isolated on temporary branches (`agentic/task/<task_id>`).
* Mission Control allows visual diff inspection (`GET /api/worktrees/diff`).
* Changes can be merged to `main` with explicit confirmation (`POST /api/worktrees/apply`) or discarded cleanly (`POST /api/worktrees/reject`).

---

## 13. Mission Control Operations Cockpit

Mission Control (`ui/dashboard/dashboard.py`) is a real-time reactive web application running on `http://127.0.0.1:3333`:

* **Live Task Lifecycle Tracker:** Real-time progress tracker (`Queued` → `Routing` → `Memory` → `Execution` → `Handoff` → `Complete`) with live elapsed stopwatch (`00:21`) and failover status.
* **Interactive Kanban Board:** Drag/inspect tasks across Queued, In Progress, Review, and Completed columns.
* **Interactive Routing Simulator:** Test the 8-factor routing engine directly from the UI.
* **Interactive Memory Search:** Run BM25 search queries and insert memory entries on the fly.
* **Hardware & Token Telemetry:** Live CPU/memory gauges, token counts distinguishing **Known Tokens** from **Estimated Tokens**, and agent health monitors.
* **Zero Build Steps:** Built with clean native HTML5, CSS custom properties, and modern ES6 with automatic 3-second polling, a live SSE event stream (`/api/events/stream`), and toast notifications.

---

## 14. Hardware Requirements & Concurrency Limits

Optimized specifically for resource-constrained environments (e.g. 2 CPU cores, 16 GB RAM):

```ini
MAX_CONCURRENT_AGENTS=2
MAX_HEAVY_AGENTS=1
```

* **Non-Blocking Execution:** Swarm workers execute in controlled daemon threads.
* **Process Discipline:** No runaway background loops or lingering orphan processes.
* **Memory Footprint:** Brain daemon consumes <60 MB RAM.

---

## 15. Security & Isolation Model

* **Strict Localhost Binding:** Mission Control binds exclusively to `127.0.0.1`. Remote network binding (`0.0.0.0`) is prohibited.
* **Bearer Token Authentication:** Mutating actions (`/api/dispatch`, `/api/execute`, `/api/tasks/cancel`, `/api/memory/add`, `/api/worktrees/*`) require Bearer token authentication stored in `runtime/mission_control.token` (permissions `0600`).
* **Rate Limiting:** Sliding-window rate limiter enforces max 30 requests/minute on execution endpoints.
* **Zero Secret Leakage:** Prompts, logs, and telemetry redact API keys, private tokens, and passwords.
* **CORS Restrictions:** Restricts origins to local loopback hosts (`127.0.0.1`, `localhost`, `::1`, any port).

---

## 16. Installation & Prerequisites

### Prerequisites
* Linux (CachyOS, Arch, Ubuntu, Debian, Fedora) or macOS
* Python 3.10+
* Git 2.30+
* Installed CLI agents (optional, according to desired providers):
  * Google Antigravity (`agy`)
  * Kiro CLI (`kiro-cli`)
  * Cline CLI (`cline`)

### Clone & Setup
```bash
git clone https://github.com/YashJadhav1023/Agentic-Brain.git
cd Agentic-Brain

# Copy environment configuration
cp .env.example .env

# Run the test suite (zero dependencies). `-t .` is required: without it the
# tests cannot import the project packages and discovery reports import errors.
python3 -m unittest discover -s tests -t .

# Hermetic end-to-end smoke test of the CLI, dashboard API and memory store
python3 scripts/smoke_test.py
```

---

## 17. Configuration & Portability

All provider and agent paths are configured in [`config/providers.json`](./config/providers.json). Paths utilize portable `~` expansion:

```json
{
  "providers": {
    "antigravity": {
      "command": "~/.gemini/bin/agy",
      "accounts": {
        "antigravity-account-1": {
          "execution": { "app_data_dir": "antigravity-cli" }
        },
        "antigravity-account-2": {
          "execution": { "app_data_dir": "antigravity-ide" }
        }
      }
    },
    "kiro": { "command": "~/.local/bin/kiro-cli" },
    "cline": { "command": "~/.local/bin/cline" }
  }
}
```

---

## 18. Running the Brain CLI

The unified CLI entrypoint is `scripts/brain.py`:

```bash
# Check agent health
python3 scripts/brain.py health

# List registered multi-agent execution resources
python3 scripts/brain.py agents

# Simulate routing for an instruction
python3 scripts/brain.py route "Design event streaming architecture"

# Plan and queue a task
python3 scripts/brain.py plan "Refactor database connection pool"

# Execute queued tasks in the swarm (runs the real agent CLIs)
python3 scripts/brain.py execute

# Preview what a Universal Continue would do, without running an agent
python3 scripts/brain.py continue --dry-run

# Universal Continue from latest handoff / state (runs the real agent CLIs)
python3 scripts/brain.py continue

# View task status
python3 scripts/brain.py status
```

### Claude accounts & the pxpipe token proxy

Claude Code accounts are first-class execution resources. Each account is isolated
by its own `CLAUDE_CONFIG_DIR`, and can authenticate with a **Pro/Max
subscription**, an OAuth token, or an API key — subscription is the default, and
in that mode **the brain holds no credential at all**: logging in, refresh and
revocation stay with the official `claude` CLI, and only login metadata (plan,
expiry) is read so health can be reported.

```bash
# List Claude accounts with auth mode, plan and pxpipe routing
python3 scripts/brain.py claude list

# Add a second subscription account (creates a 0700 profile dir, prints the login command)
python3 scripts/brain.py claude add --name account-2 --use-pxpipe

# Print the interactive login command for an account
python3 scripts/brain.py claude login claude-account-2

# Login + health state, with no credential exposure
python3 scripts/brain.py claude status --json
```

Optionally route those accounts through [pxpipe](https://github.com/teamchong/pxpipe)
(MIT), a loopback proxy that renders bulky request context as dense PNG pages so
token-heavy context costs image tokens instead of text tokens. Measured on a real
request here: **116,185 characters of context became 30,958 characters plus 16
image pages.**

```bash
python3 scripts/brain.py pxpipe status     # installed? running? from where?
python3 scripts/brain.py pxpipe start      # loopback only, on 127.0.0.1:47821
python3 scripts/brain.py pxpipe savings    # measured savings from the event log
python3 scripts/brain.py pxpipe stop
```

pxpipe is **opt-in per account**, because imaged content is lossy for exact
strings: a misread is a plausible wrong value rather than an error. Full setup,
security properties, measurement rules and troubleshooting:
**[docs/CLAUDE_ACCOUNTS.md](./docs/CLAUDE_ACCOUNTS.md)**.

---

## 19. Running Mission Control

Launch the real-time Mission Control dashboard:

```bash
python3 ui/dashboard/dashboard.py
# or, equivalently
python3 scripts/brain.py dashboard --port 3333
```

* **URL:** `http://127.0.0.1:3333` (override with `BRAIN_PORT` or `--port`)
* **Auth Token:** Generated on first start in `runtime/mission_control.token` (mode `0600`). The web UI fetches it from the loopback-only `GET /api/token` endpoint and sends it as `Authorization: Bearer <token>`; the SSE stream accepts it as `?token=`.

---

## 20. Development, Benchmarks & Testing

### Running the Test Suite
```bash
python3 -m unittest discover -s tests -t .        # everything
python3 -m unittest discover -s tests/unit -t .   # unit tests only
```

### Running the End-to-End Smoke Test
```bash
python3 scripts/smoke_test.py            # all groups; exits non-zero on any failure
python3 scripts/smoke_test.py -v         # show evidence for passing checks too
python3 scripts/smoke_test.py --only dashboard --only cli
python3 scripts/smoke_test.py --keep     # keep the sandbox (and dashboard.log) for inspection
```
*Hermetic: copies the tracked files into a temporary directory, redirects `HOME`,
`BRAIN_DIR`, `XDG_*` and `BRAIN_PROVIDERS_CONFIG` into it, and puts shims for
`agy`, `kiro-cli`, `cline` and `secret-tool` first on `PATH`. The shims answer
`--version` and refuse everything else, so no real agent, OAuth flow or OS keyring
is ever touched, and your checkout, `~/agentic-brain` and `~/.gemini` are never
written. It starts the dashboard on `127.0.0.1` on an ephemeral port, hits every
route (happy path plus a bad input each, including the SSE stream), runs every
`scripts/brain.py` subcommand, and round-trips memory, tasks, jobs and handoffs.
Standard library only; takes a few minutes.*

### Running the Optimization Benchmark Suite
```bash
python3 scripts/benchmark.py --json
```
*Executes tests A through G covering context optimization, routing score explainability, continuation budgets, single-depth verification, and failover.*

### End-to-End Autonomous Pipeline Demonstration
```bash
python3 scripts/live_pipeline_demonstration.py
```
*Not hermetic: this runs `Orchestrator.execute_next()`, i.e. a real agent CLI with your configured accounts, against this checkout.
Executes a live end-to-end task through routing, targeted memory retrieval, context optimization, headless AG-2 execution, handoff, continuation, and failover verification.*

---

## 21. Universal AI Mission Control (Phases 16–20)

Phases 16–20 establish the universal intelligent control plane across all local and connected systems:

1. **Universal Resource & Tool Intelligence (Phase 16)**: Discovers and catalogs 11 MCP servers, 26 MCP tools with automatic safety classification, 86 AI skills, 17 CLI utilities, steering docs, and repositories into an indexed catalog of 232 items (`brain resources`, `brain tools`, `brain docs`).
2. **Unified Context & Knowledge Federation (Phase 17)**: Assembles agent-scoped execution contexts with BM25 knowledge retrieval, provenance tracking, and leak-proof credential redaction (`brain context preview`, `brain context explain`).
3. **Autonomous Task Planning & Execution Graphs (Phase 18)**: Decomposes tasks into dependency DAGs with human-in-the-loop approval gates for destructive actions, outcome verification, and LIFO rollback compensations (`brain plan`).
4. **Dynamic Self-Optimization & Execution Fabric (Phase 19)**: Empirical telemetry in `PerformanceRegistry`, closed-loop affinity boosts, and deep explainability reports (`brain route explain`, `brain metrics`).
5. **Universal Governance & Audit (Phase 20)**: Unified `MissionBrain` facade and immutable append-only audit trail (`brain health --deep`, `brain audit`).

### Running Universal Brain CLI Commands:
```bash
# Comprehensive cluster health diagnostics
python3 scripts/brain.py health --deep

# Explain routing with candidate evaluations and recommended resources
python3 scripts/brain.py route explain "Refactor auth tokens across services"

# Preview synthesized context without execution
python3 scripts/brain.py context preview "Audit repository security"

# Create and inspect autonomous multi-step plan
python3 scripts/brain.py plan "Delete old logs and compile report"

# View empirical performance telemetry
python3 scripts/brain.py metrics

# Inspect immutable governance audit log
python3 scripts/brain.py audit --limit 20
```

---

## License

Apache License 2.0. See [LICENSE](./LICENSE) for details.
