#!/usr/bin/env python3
"""Register the shared 'brain' memory MCP server into every local agent config.

Idempotent: re-running updates the brain entry and leaves all other servers
(and their credentials) byte-for-byte untouched. Every file is backed up to
<file>.bak-<timestamp> before the first write.

Targets:
  ~/.kiro/settings/mcp.json                                                              Kiro CLI (global) + Kiro VS Code ext
  <repo>/.kiro/settings/mcp.json                                                         Kiro CLI (workspace)
  ~/.aws/amazonq/mcp.json                                                                Amazon Q CLI + Amazon Q VS Code ext
  ~/.gemini/config/mcp_config.json                                                       Antigravity IDE + Antigravity CLI
  ~/.config/Code/User/mcp.json                                                           VS Code native MCP (Copilot agent)
  ~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json Cline (VS Code ext globalStorage)
  ~/.cline/data/settings/cline_mcp_settings.json                                         Cline (standalone / CLI data)
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parents[2]

BRAIN_PROJECT = "brain"
# basic-memory resolves its data dir from BASIC_MEMORY_CONFIG_DIR, then
# XDG_CONFIG_HOME/basic-memory, then ~/.basic-memory. Agents launched from a
# desktop session and from a bare shell disagree on XDG_CONFIG_HOME, which split
# the index in two (notes/two-basic-memory-homes-split-the-brain-index). Every
# entry pins the same dir the `brain` CLI pins, so re-running this script can no
# longer strip a hand-applied pin and reintroduce the split.
# Derived from HOME only, deliberately: an inherited BASIC_MEMORY_CONFIG_DIR (the
# `brain` wrapper exports one) would otherwise let a run under a throwaway HOME,
# such as the e2e suite, rewrite the real basic-memory config.
BASIC_MEMORY_CONFIG_DIR = str(HOME / ".config" / "basic-memory")
BRAIN_DIR = HOME / "agentic-brain"
BRAIN_BIN = str(HOME / ".local" / "bin" / "basic-memory")
AGENT_PATH = f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Read + write tools are pre-approved so a handoff never stalls on a prompt.
# Destructive tools (delete_note, delete_project, move_note) are deliberately
# excluded so they still require explicit confirmation.
AUTO_APPROVE = [
    "read_note",
    "view_note",
    "read_content",
    "search_notes",
    "search",
    "fetch",
    "recent_activity",
    "build_context",
    "list_directory",
    "list_memory_projects",
    "list_workspaces",
    "write_note",
    "edit_note",
]


def brain_entry(*, dialect: str) -> dict:
    """Build the server entry, matching each client's config dialect."""
    entry = {
        "command": BRAIN_BIN,
        # --project pins every agent to the same store explicitly. Relying on
        # the config default is not enough: basic-memory resolves the default
        # from ~/.basic-memory/config.json, which drifts from the project DB.
        "args": ["mcp", "--project", BRAIN_PROJECT],
        "env": {
            "BASIC_MEMORY_DEFAULT_PROJECT": BRAIN_PROJECT,
            "BASIC_MEMORY_CONFIG_DIR": BASIC_MEMORY_CONFIG_DIR,
            "PATH": AGENT_PATH,
        },
    }
    if dialect == "kiro":  # Kiro / Amazon Q understand disabled + autoApprove
        entry["disabled"] = False
        entry["autoApprove"] = list(AUTO_APPROVE)
    elif dialect == "vscode":  # VS Code requires an explicit transport type
        entry["type"] = "stdio"
    return entry


TARGETS = [
    # (path, dialect, container-key-path)
    (HOME / ".kiro/settings/mcp.json", "kiro", ("mcpServers",)),
    (REPO / ".kiro/settings/mcp.json", "kiro", ("mcpServers",)),
    (HOME / ".aws/amazonq/mcp.json", "kiro", ("mcpServers",)),
    (HOME / ".gemini/config/mcp_config.json", "plain", ("mcpServers",)),
    # VS Code >=1.102 keeps MCP servers here, not in settings.json. Writing to
    # settings.json makes VS Code migrate the entry here and null out the key,
    # so target the real file directly.
    (HOME / ".config/Code/User/mcp.json", "vscode", ("servers",)),
    (
        HOME
        / ".config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        "plain",
        ("mcpServers",),
    ),
    (HOME / ".cline/data/settings/cline_mcp_settings.json", "plain", ("mcpServers",)),
]


def load_json(path: Path) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def dig(root: dict, keys: tuple[str, ...]) -> dict:
    node = root
    for key in keys:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise TypeError(f"expected object at {'.'.join(keys)}")
    return node


def ensure_brain_project() -> None:
    """Guarantee <BASIC_MEMORY_CONFIG_DIR>/config.json can resolve the brain project.

    `basic-memory project add` records the project in its SQLite DB but does not
    always write it into config.json, and the MCP server resolves a project name
    to a path via config.json. Without this, a server launched with a clean
    environment (which is how every agent launches it) exits with
    "No project found named: brain".
    """
    # The pinned data dir, not ~/.basic-memory: that is the home the MCP servers
    # read, so it is the one that must resolve the brain project.
    cfg = Path(BASIC_MEMORY_CONFIG_DIR) / "config.json"
    brain_dir = BRAIN_DIR
    brain_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.exists():
        print(f"WARN  {cfg} missing; run `basic-memory project list` once first")
        return

    data = json.loads(cfg.read_text(encoding="utf-8"))
    projects = data.setdefault("projects", {})
    if projects.get(BRAIN_PROJECT, {}).get("path") == str(brain_dir):
        print(f"OK     brain project -> {brain_dir}")
        return

    shutil.copy2(cfg, cfg.with_name(f"config.json.bak-{time.strftime('%Y%m%d-%H%M%S')}"))
    projects[BRAIN_PROJECT] = {
        "path": str(brain_dir),
        "mode": "local",
        "workspace_id": None,
        "local_sync_path": None,
        "bisync_initialized": False,
        "last_sync": None,
    }
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"ADD    brain project -> {brain_dir}")


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    failures = 0

    ensure_brain_project()

    for path, dialect, keys in TARGETS:
        label = str(path).replace(str(HOME), "~")
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
            print(f"NEW   {label}")

        try:
            data = load_json(path)
        except json.JSONDecodeError as exc:
            print(f"FAIL  {label}: invalid JSON, left untouched ({exc})")
            failures += 1
            continue

        backup = path.with_name(f"{path.name}.bak-{stamp}")
        shutil.copy2(path, backup)

        container = dig(data, keys)
        action = "UPDATE" if BRAIN_PROJECT in container else "ADD   "
        container[BRAIN_PROJECT] = brain_entry(dialect=dialect)

        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        # Re-read to prove we did not corrupt the file.
        load_json(path)
        print(f"{action} {label}  (backup: {backup.name})")

    if failures:
        print(f"\n{failures} file(s) failed.")
        return 1
    print("\nAll agent configs now point at the same brain.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
