# Architecture (internal short form)

> The canonical architecture document is **[../ARCHITECTURE.md](../ARCHITECTURE.md)**
> at the repository root. It additionally covers shared-memory storage and sync,
> setup on a fresh clone, configuration, and the MCP servers in use.
> This file is a condensed internal note on execution path, layout and design rules.

## Execution path

```
USER
 |
 v
BRAIN CLI  (scripts/brain.py)
 |
 v
ORCHESTRATOR  (brain/orchestrator/orchestrator.py)
 |
 +--> SMART ROUTER      (brain/router/smart_router.py)      scored competition
 |                                                          across all agents
 +--> TASK MANAGER      (tasks/manager.py)                   persistent record
 |
 v
SWARM WORKER POOL  (brain/orchestrator/swarm.py)  bounded concurrency = 2
 |
 +--> health pre-flight        (fail fast, never burn a run on a dead account)
 +--> file locks               (locks/file_locker.py)
 +--> relevant memory only     (memory/retrieval/retriever.py)
 |
 v
AGENT ADAPTER  (agents/<provider>/adapter.py)
 |
 v
CLI SUBPROCESS   e.g. agy --app_data_dir=antigravity-ide --output-format json -p
 |
 v
STRUCTURAL JSON NORMALIZER  ->  TaskExecutionResult
 |
 +--> TASK RECORD      tasks/{queue,active,completed,failed}/<task-id>.json
 +--> SESSION MAPPING  sessions/session_registry.json
 +--> SHARED MEMORY    memory/store/shared_memory.db
 +--> HANDOFF          handoffs/current.md + current.json
 +--> EVENTS           runtime/logs/events.jsonl
 |
 v
MISSION CONTROL  (ui/dashboard, 127.0.0.1:3333)
 |
 v
NEXT AGENT  (brain.py continue)
```

## Layout

| Path | Responsibility |
|---|---|
| `agents/base/adapter.py` | `AgentAdapter` contract, `Capability`, `AgentStatus`, `TaskExecutionResult`, `UNKNOWN_MODEL` |
| `agents/antigravity/` | Provider factory + per-account adapter; `account1/`, `account2/` are named config handles |
| `agents/kiro/`, `agents/cline/` | Single-account CLI adapters |
| `config/providers.json` | The only configuration source |
| `providers/registry/` | Config loader, `Provider`, `ProviderRegistry`, bootstrap |
| `models/policies/model_policy.py` | Verified catalogues, deterministic selection, honest verification |
| `brain/router/` | Capability-aware scored routing |
| `brain/orchestrator/` | Orchestrator + bounded swarm pool |
| `brain/context/continuator.py` | Universal Continue |
| `tasks/` | Persistent task lifecycle |
| `sessions/` | task -> session -> conversation mapping |
| `memory/` | Shared SQLite store + relevance retrieval |
| `providers/mcp/` | Phase 15: Read-only MCP discovery, tool catalog, safety classification |
| `brain/knowledge/` | Phase 15: Steering registry, precedence hierarchy, document registry, relevance |
| `brain/resources/` | Phase 15: CLI registry, repository awareness, Universal Resource Graph |
| `brain/context/` | Phase 15: Task context builder, auto resource selection, preview |
| `handoffs/` | Markdown + JSON handoff records and archive |
| `events/` | Append-only JSONL event bus |
| `locks/` | Atomic file locks with TTL |
| `ui/dashboard/` | Mission Control (loopback only) + Phase 15 MCP/Tools/Context API |
| `tests/` | Unit and integration test suites |

## Design rules

1. **Provider-agnostic core.** Nothing above the adapter layer knows what a CLI
   flag is. Adding a provider touches config plus one adapter.
2. **One command builder per provider.** `--app_data_dir` appears in exactly one
   function, which is what makes account isolation structural rather than
   conventional.
3. **Never fabricate.** If a provider does not report the model, the model is
   `unknown`. If a run produces no output, it failed.
4. **Least privilege by default.** Permission escalation is opt-in, per task or
   per account, and is visible in the task record and the UI.
5. **Bounded resources.** `MAX_CONCURRENT_AGENTS=2` for this 2-core / 16 GB
   host, from config, overridable by environment.
6. **Durable state, no daemons.** Plain JSON/JSONL/SQLite files on disk, so the
   system survives a crash and can be inspected without running anything.
