#!/usr/bin/env python3
"""End-to-end proof that the brain is genuinely shared across agent processes.

Simulates the exact failure the brain exists to solve:

  1. Agent A (its own MCP server process) writes a handoff checkpoint, then dies.
  2. Agent B (a completely separate MCP server process, as a different client)
     starts cold, reads recent_activity and the checkpoint, and recovers the
     task state.
  3. The note is a plain Markdown file on disk, readable by humans and by the
     `brain` CLI.
  4. Agent B appends its own progress, and Agent C sees it.

Nothing is shared between the processes except ~/agentic-brain, so a pass means
context really does survive an agent dying.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

BM = str(Path.home() / ".local" / "bin" / "basic-memory")
BRAIN_DIR = Path.home() / "agentic-brain"
NOTE_PATH = BRAIN_DIR / "handoff" / "current.md"

CANARY = "CANARY-8f3a1c-cross-agent"

CHECKPOINT = f"""---
title: current
type: handoff
tags: [handoff, active]
---

# Wire a single shared brain into every local AI agent

## Observations
- [status] in-progress
- [agent] agent-a-simulated
- [scope] /home/setoo/YashDevops/Agentic_os
- [done] brain MCP server registered in all 5 agent config files
- [next] verify agent B can read this note from a cold start
- [file] scripts/brain/register-brain-mcp.py registers the MCP server
- [command] python3 scripts/brain/register-brain-mcp.py
- [blocker] none
- [decision] basic-memory chosen over ByteRover: local, no LLM key, plain Markdown
- [canary] {CANARY}

## Relations
- relates_to [[projects/agentic-os]]
"""


class Agent:
    """One agent session: a long-lived MCP client over its own server process.

    Mirrors how a real agent behaves — the process stays up for the whole
    session and is torn down when the agent "runs out of budget".
    """

    ENV = {
        "PATH": f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "BASIC_MEMORY_DEFAULT_PROJECT": "brain",
    }

    def __init__(self, name: str) -> None:
        self.name = name
        self._id = 1
        self.proc = subprocess.Popen(
            [BM, "mcp", "--project", "brain"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=self.ENV,
        )
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": self.name, "version": "1"},
            },
        )
        self._notify("notifications/initialized")

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def _request(self, method: str, params: dict) -> dict:
        self._id += 1
        want = self._id
        self._send(
            {"jsonrpc": "2.0", "id": want, "method": method, "params": params}
        )
        assert self.proc.stdout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.name}: server closed during {method}")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != want:
                continue  # skip notifications and unrelated responses
            if "error" in msg:
                raise RuntimeError(f"{self.name}/{method}: {msg['error']}")
            return msg.get("result", {})

    def call(self, tool: str, args: dict) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": args})
        if result.get("isError"):
            raise RuntimeError(f"{self.name}/{tool}: {result}")
        return "\n".join(
            c.get("text", "")
            for c in result.get("content", [])
            if c.get("type") == "text"
        )

    def die(self) -> None:
        """The agent exhausts its token budget and its process goes away."""
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
                self.proc.wait(timeout=10)


results: list[tuple[bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    results.append((passed, label))
    mark = "PASS" if passed else "FAIL"
    print(f"[{mark}] {label}")
    if detail and not passed:
        print(f"       {detail}")


def reindex() -> None:
    """Make the database agree with what is on disk."""
    subprocess.run(
        [BM, "reindex", "-p", "brain", "--full"],
        capture_output=True,
        text=True,
        timeout=300,
    )


def borrow_live_baton() -> str | None:
    """Step 1 overwrites handoff/current, which may be a real in-flight handoff.

    Return its contents so they can be put back, or None if no baton exists.
    """
    if not NOTE_PATH.exists():
        return None
    saved = NOTE_PATH.read_text(encoding="utf-8")
    print(f"[INFO] live baton found at {NOTE_PATH.name}; it will be restored afterwards")
    return saved


def return_live_baton(saved: str | None) -> None:
    """Put the real baton back, or clear the test artifact if there was none."""
    if saved is None:
        if NOTE_PATH.exists():
            NOTE_PATH.unlink()
            print("[INFO] removed test baton; no real handoff was in flight")
    else:
        NOTE_PATH.write_text(saved, encoding="utf-8")
        print("[INFO] restored the live baton that was in flight before the test")
    reindex()


def _run_checks() -> int:
    print("=" * 74)
    print("STEP 1  Agent A writes a checkpoint, then its process ends")
    print("=" * 74)
    a = Agent("agent-a-kiro-cli")
    a.call(
        "write_note",
        {
            "title": "current",
            "directory": "handoff",
            "content": CHECKPOINT,
            "note_type": "handoff",
            "overwrite": True,
        },
    )
    check("agent A wrote handoff/current via MCP", True)
    a.die()
    check("agent A's process is gone (token budget exhausted)", a.proc.poll() is not None)

    print()
    print("=" * 74)
    print("STEP 2  Agent B starts cold in a separate process and recovers state")
    print("=" * 74)
    b = Agent("agent-b-amazon-q")

    activity = b.call("recent_activity", {"timeframe": "1d"})
    check(
        "agent B sees the checkpoint in recent_activity",
        "current" in activity or "handoff" in activity.lower(),
        activity[:300],
    )

    note = b.call("read_note", {"identifier": "handoff/current"})
    check("agent B can read_note handoff/current", bool(note.strip()))
    check(
        "agent B recovered the canary written by agent A",
        CANARY in note,
        note[:300],
    )
    check(
        "agent B recovered the concrete next step",
        "verify agent B can read this note from a cold start" in note,
    )
    check(
        "agent B recovered the decision it must not reverse",
        "basic-memory chosen over ByteRover" in note,
    )

    found = b.call("search_notes", {"query": CANARY})
    check("checkpoint is semantically searchable", CANARY in found or "current" in found)

    print()
    print("=" * 74)
    print("STEP 3  The brain is plain Markdown on disk")
    print("=" * 74)
    check(f"file exists at {NOTE_PATH.name}", NOTE_PATH.exists(), str(NOTE_PATH))
    on_disk = NOTE_PATH.read_text(encoding="utf-8") if NOTE_PATH.exists() else ""
    check("file on disk contains the canary", CANARY in on_disk)

    cli = subprocess.run(
        [str(Path.home() / ".local/bin/brain"), "resume"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        "`brain resume` CLI prints the same briefing",
        CANARY in cli.stdout,
        cli.stdout[:200] + cli.stderr[:200],
    )

    print()
    print("=" * 74)
    print("STEP 4  Agent B appends progress, Agent C picks it up")
    print("=" * 74)
    b.call(
        "edit_note",
        {
            "identifier": "handoff/current",
            "operation": "append",
            "content": "\n- [progress] agent-b-amazon-q resumed and continued the task\n",
        },
    )
    c = Agent("agent-c-antigravity")
    note_c = c.call("read_note", {"identifier": "handoff/current"})
    check(
        "agent C sees agent B's appended progress",
        "agent-b-amazon-q resumed and continued" in note_c,
        note_c[-300:],
    )
    check("agent C still sees agent A's original context", CANARY in note_c)
    b.die()
    c.die()

    print()
    print("=" * 74)
    failed = [label for ok, label in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for label in failed:
            print(f"  FAILED: {label}")
        return 1
    print("Context survives an agent dying. The brain is shared.")
    return 0


def main() -> int:
    """Run the checks without destroying a real handoff that is in flight."""
    saved = borrow_live_baton()
    try:
        return _run_checks()
    finally:
        return_live_baton(saved)


if __name__ == "__main__":
    sys.exit(main())
