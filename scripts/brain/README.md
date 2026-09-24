# Shared brain

One MCP server, named `brain`, gives every AI agent on this machine the same
persistent memory. When an agent exhausts its token budget, you switch to another
and it resumes the same task from the same state.

Store: `~/agentic-brain` — plain Markdown, local, no cloud, no second LLM.

## Setup (already applied, safe to re-run)

```bash
uv tool install basic-memory
python3 scripts/brain/register-brain-mcp.py   # register the server in every agent
python3 scripts/brain/install-protocol.py     # install the read/write rules
python3 scripts/brain/test-handoff.py         # prove context survives a dead agent
```

Both registration scripts are idempotent and back up every file they touch.

## Using it

The protocol is installed into each agent's rules, so agents should read and
write the brain unprompted. To drive it explicitly:

- Dying agent: **"checkpoint to the brain"**
- Fresh agent: **"resume from the brain"**

Between agents, from any shell:

```bash
brain status      # what is in flight
brain resume      # the current briefing
brain log         # recent activity across all agents
brain find "..."  # semantic search
brain archive x   # retire the current checkpoint
```

## Which file wires which agent

| Agent surface | Config |
| --- | --- |
| Kiro CLI, Amazon Q CLI (alias of Kiro here) | `~/.kiro/settings/mcp.json`, `~/.aws/amazonq/mcp.json` |
| Kiro VS Code extension | `<repo>/.kiro/settings/mcp.json` |
| Amazon Q VS Code extension | `~/.aws/amazonq/mcp.json` |
| Antigravity IDE + Antigravity CLI | `~/.gemini/config/mcp_config.json` |
| VS Code / Copilot agent mode | `~/.config/Code/User/mcp.json` |

Verify any of them with `kiro-cli mcp list` or `antigravity mcp list`.

## Notes for whoever changes this

- The server is launched as `basic-memory mcp --project brain`. The `--project`
  flag is required: basic-memory rewrites `default_project` in
  `~/.basic-memory/config.json` on startup, so relying on the default lets
  agents drift into a different store.
- `write_note` takes `directory` and `note_type`. Not `folder`, not
  `entity_type`. The CLI, confusingly, uses `--folder`.
- `move_note`, `delete_note` and `delete_project` are intentionally left out of
  `autoApprove` so destructive brain edits still require confirmation.
- Never write credential values into brain notes. They are plain files that
  every agent reads.
