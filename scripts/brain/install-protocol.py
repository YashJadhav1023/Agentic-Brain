#!/usr/bin/env python3
"""Install the shared-brain handoff protocol into every agent's rules location.

One source of truth (protocol/shared-brain.md) is distributed to each agent's
own rules mechanism, because no two of these tools read the same file:

  ~/.kiro/steering/shared-brain.md            Kiro CLI, all workspaces
  <repo>/.kiro/steering/shared-brain.md       Kiro CLI + Kiro VS Code ext
  <repo>/.amazonq/rules/shared-brain.md       Amazon Q CLI + Q VS Code ext
  <repo>/AGENTS.md                            Antigravity IDE + Antigravity CLI
  <repo>/.github/copilot-instructions.md      VS Code Copilot agent mode
  <repo>/.clinerules                          Cline agent rules

Files owned entirely by this protocol are overwritten. Shared files (AGENTS.md,
copilot-instructions.md, .clinerules) get a delimited managed block so
hand-written content around it survives re-runs.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent / "protocol" / "shared-brain.md"

BEGIN = "<!-- BEGIN SHARED BRAIN PROTOCOL - managed by scripts/brain/install-protocol.py -->"
END = "<!-- END SHARED BRAIN PROTOCOL -->"

KIRO_FRONTMATTER = "---\ninclusion: always\n---\n\n"

# (path, mode, frontmatter)
#   own   -> this file exists only for the protocol; overwrite it
#   block -> file may hold other content; maintain a delimited block
TARGETS = [
    (HOME / ".kiro/steering/shared-brain.md", "own", KIRO_FRONTMATTER),
    (REPO / ".kiro/steering/shared-brain.md", "own", KIRO_FRONTMATTER),
    (REPO / ".amazonq/rules/shared-brain.md", "own", ""),
    (REPO / "AGENTS.md", "block", ""),
    (REPO / ".github/copilot-instructions.md", "block", ""),
    (REPO / ".clinerules", "own", ""),
]


def upsert_block(existing: str, body: str) -> str:
    block = f"{BEGIN}\n\n{body.rstrip()}\n\n{END}\n"
    if BEGIN in existing and END in existing:
        head, rest = existing.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        return f"{head}{block}{tail.lstrip(chr(10))}"
    if existing.strip():
        return f"{existing.rstrip()}\n\n{block}"
    return block


def main() -> int:
    if not SOURCE.exists():
        print(f"missing source: {SOURCE}")
        return 1

    body = SOURCE.read_text(encoding="utf-8")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    check_mode = "--check" in sys.argv
    all_ok = True

    for path, mode, frontmatter in TARGETS:
        label = str(path).replace(str(HOME), "~")
        if mode == "own":
            expected = frontmatter + body
        else:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            expected = upsert_block(existing, body)

        if check_mode:
            if not path.exists() or path.read_text(encoding="utf-8") != expected:
                print(f"DIFF   {label}")
                all_ok = False
            else:
                print(f"OK     {label}")
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_text(encoding="utf-8") == expected:
                print(f"OK     {label} (unchanged)")
                continue
            shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
            action = "UPDATE"
        else:
            action = "CREATE"

        path.write_text(expected, encoding="utf-8")
        print(f"{action} {label}")

    if check_mode:
        if all_ok:
            print("\nAll agent steering files are in sync.")
            return 0
        print("\nSteering drift detected. Run without --check to fix.")
        return 1

    print("\nHandoff protocol installed for all agents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
