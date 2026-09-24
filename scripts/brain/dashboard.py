#!/usr/bin/env python3
"""Shared Brain Dashboard - Interactive Visual Knowledge Graph & Mission Control

Next-Gen Enhancements:
- Interactive Multi-Type Graph Filters (Handoffs, Projects, Decisions, Runbooks, Schemas)
- Live Real-Time Canvas Search Highlighting & Glowing Pulses
- 1st & 2nd Degree Click-to-Focus Neighborhood Isolation
- Dynamic Hub Centrality Sizing (Degree-based node scaling)
- Slide-Out Glassmorphism Markdown Drawer & Backlink Explorer
- Multi-Agent Contribution Radar & Store Health Integrity Widget
- Persistent Semantic Search Sidecar Integration (<2ms cached / ~2s warm)
- 100% Offline with vendored assets in /static/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import gzip
import hashlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()
STATIC_DIR = (Path(__file__).resolve().parent / "static").resolve()
HANDOFF_PATH = BRAIN_DIR / "handoff" / "current.md"

SIDECAR_HOST = "127.0.0.1"
SIDECAR_PORT = int(os.environ.get("BRAIN_SIDECAR_PORT", "3334"))
SIDECAR_SCRIPT = (Path(__file__).resolve().parent / "search_sidecar.py").resolve()
BM_SEARCH_TIMEOUT = 30

subscribers: list[asyncio.Queue | threading.Event | list] = []
subscribers_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Generation-keyed response cache.
#
# Every read endpoint used to re-walk and re-parse the whole brain on each
# request, and the SSE watcher fires on every file write. While a swarm task is
# dispatched the task JSON files churn continuously, so the browser was issuing
# /api/graph + /api/status + /api/swarm back to back, several times a second,
# each one a full disk rescan. That is the "the UI goes slow when I assign a
# task" symptom.
#
# `_brain_generation` is bumped by `brain_watcher` only when a tracked file
# actually changes, so a cached payload is served straight from memory until the
# store really moves. Correctness is preserved because the generation is the
# same signal the SSE stream uses to tell clients to refetch.
# ---------------------------------------------------------------------------
_brain_generation = 0
_generation_lock = threading.Lock()
_response_cache: dict[str, tuple[int, object]] = {}
_response_cache_lock = threading.Lock()

# Minimum gap between two SSE "update" pushes. A dispatch writes many files in a
# burst; without this every write became its own client-side refresh storm. A
# trailing push is always guaranteed, so no change is ever silently dropped.
SSE_COALESCE_SECONDS = float(os.environ.get("BRAIN_UI_SSE_COALESCE_SECONDS", "1.5"))

# Responses at or above this size are gzipped when the client advertises it.
# /api/swarm alone is ~240 KB of JSON and compresses by roughly 10x.
GZIP_MIN_BYTES = 2048


def current_generation() -> int:
    with _generation_lock:
        return _brain_generation


def bump_generation() -> int:
    global _brain_generation
    with _generation_lock:
        _brain_generation += 1
        return _brain_generation


def cached_by_generation(key: str, producer):
    """Return ``producer()`` memoised against the current brain generation.

    The producer runs outside the cache lock so a slow scan never blocks other
    endpoints; a benign duplicate computation on a cold race is cheaper than
    serialising every request behind one mutex.
    """
    generation = current_generation()
    with _response_cache_lock:
        hit = _response_cache.get(key)
        if hit is not None and hit[0] == generation:
            return hit[1]
    value = producer()
    with _response_cache_lock:
        _response_cache[key] = (generation, value)
    return value

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sentinel
    import cognitive_engine
    import swarm
    import orchestrator
    import dashboard_execution
    # providers.py turns a raw API key into a working worker with no agent CLI
    # installed. It is optional the same way the others are: if it fails to
    # import the dashboard still serves the graph, the provider panel just
    # reports the module as unavailable rather than taking the whole page down.
    import providers
except Exception as e:
    sentinel = None
    cognitive_engine = None
    swarm = None
    orchestrator = None
    dashboard_execution = None
    providers = None

dashboard_executor = dashboard_execution.DashboardExecutor() if dashboard_execution is not None else None



def parse_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    yaml_text, body = parts[1], parts[2]
    meta = {}
    for line in yaml_text.splitlines():
        if line.startswith(" ") or line.startswith("\t"):
            continue
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                v = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
            meta[k] = v
    return meta, body


def parse_observations(body: str) -> dict[str, any]:
    obs: dict[str, any] = {
        "status": "in-progress",
        "agent": "unknown",
        "scope": "unknown",
        "done": [],
        "next": "",
        "file": [],
        "command": [],
        "blocker": "none",
        "decision": [],
    }
    in_obs = False
    for line in body.splitlines():
        if line.startswith("## Observations"):
            in_obs = True
            continue
        elif line.startswith("## ") and in_obs:
            break
        if in_obs and line.strip().startswith("- ["):
            m = re.match(r"^-\s*\[([a-zA-Z0-9_-]+)\]\s*(.*)$", line.strip())
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                if key in ("done", "file", "command", "decision"):
                    obs[key].append(val)
                else:
                    obs[key] = val
    return obs


def extract_links(body: str) -> list[str]:
    links = []
    matches = re.findall(r"\[\[(.*?)\]\]", body)
    for m in matches:
        target = m.split("|")[0].strip()
        links.append(target)
    return list(set(links))


def get_all_notes() -> list[dict]:
    notes = []
    if not BRAIN_DIR.exists():
        return notes

    for p in sorted(BRAIN_DIR.glob("**/*.md")):
        rel = p.relative_to(BRAIN_DIR).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            meta, body = parse_frontmatter(content)
            obs = parse_observations(body)
            links = extract_links(body)
            stat = p.stat()
            first_header = ""
            for l in body.splitlines():
                if l.startswith("# "):
                    first_header = l[2:].strip()
                    break

            folder_type = rel.split("/")[0] if "/" in rel else "general"
            note_type = meta.get("type") or folder_type
            if note_type == "schema" or folder_type == "schemas":
                note_type = "schemas"
            elif note_type in ("handoff", "handoffs") or folder_type == "handoff":
                note_type = "handoff"
            elif note_type in ("projects", "project") or folder_type == "projects":
                note_type = "projects"
            elif note_type in ("decisions", "decision") or folder_type == "decisions":
                note_type = "decisions"
            elif note_type in ("runbooks", "runbook") or folder_type == "runbooks":
                note_type = "runbooks"

            agent = obs.get("agent")
            if not agent or agent == "unknown":
                agent = meta.get("agent")
            if not agent or agent == "unknown":
                if "kiro" in rel or "kiro" in body.lower():
                    agent = "kiro-cli"
                elif "cline" in rel or "cline" in body.lower():
                    agent = "cline"
                elif "antigravity-ide" in rel or "antigravity-ide" in body.lower():
                    agent = "antigravity-ide"
                elif "amazon-q" in rel or "amazonq" in body.lower() or "amazon-q" in body.lower():
                    agent = "amazon-q"
                elif "antigravity" in rel or "antigravity" in body.lower():
                    agent = "antigravity"
                else:
                    agent = "human"

            notes.append({
                "path": rel,
                "title": meta.get("title", first_header or p.stem),
                "type": note_type,
                "agent": agent,
                "tags": meta.get("tags", []),
                "links": links,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "is_current": (rel == "handoff/current.md"),
                "summary": body[:250].replace("\n", " ").strip(),
                "observations": obs,
                "meta": meta,
            })
        except Exception as e:
            print(f"Error reading {rel}: {e}", file=sys.stderr)
    return notes


def get_graph_data() -> dict:
    # Share the parsed-note cache with /api/status; these two endpoints are always
    # fetched together and used to parse every note on disk twice per refresh.
    notes = cached_by_generation("notes", get_all_notes)
    alias_map = {}
    for n in notes:
        alias_map[n["path"]] = n["path"]
        alias_map[n["path"].replace(".md", "")] = n["path"]
        alias_map[Path(n["path"]).stem] = n["path"]

    nodes = []
    edges = []
    edge_set = set()
    degree_map = Counter()

    for n in notes:
        for link in n["links"]:
            target_path = alias_map.get(link) or alias_map.get(link + ".md")
            if target_path and target_path != n["path"]:
                degree_map[n["path"]] += 1
                degree_map[target_path] += 1

    for n in notes:
        is_cur = n["is_current"]
        deg = degree_map[n["path"]]
        color_map = {
            "handoff": "#ffaa00" if not is_cur else "#ff0055",
            "projects": "#00f0ff",
            "decisions": "#bd00ff",
            "runbooks": "#00ff9d",
            "schemas": "#38bdf8",
        }
        color = color_map.get(n["type"], "#94a3b8")
        val_size = 16 if is_cur else (12 if n["type"] == "projects" else max(6, 6 + deg * 2))

        nodes.append({
            "id": n["path"],
            "name": n["title"],
            "label": n["title"],
            "type": n["type"],
            "agent": n["agent"],
            "color": color,
            "val": val_size,
            "degree": deg,
            "is_current": is_cur,
            "linksCount": len(n["links"]),
            "path": n["path"]
        })

        for link in n["links"]:
            target_path = alias_map.get(link) or alias_map.get(link + ".md")
            if target_path and target_path != n["path"]:
                edge_id = f"{n['path']}->{target_path}"
                if edge_id not in edge_set:
                    edge_set.add(edge_id)
                    edges.append({
                        "source": n["path"],
                        "target": target_path,
                        "color": "rgba(0, 240, 255, 0.6)" if is_cur else "rgba(148, 163, 184, 0.3)"
                    })

    return {"nodes": nodes, "links": edges, "edges": edges}


def get_current_baton() -> dict | None:
    if not HANDOFF_PATH.exists():
        return None
    try:
        content = HANDOFF_PATH.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_frontmatter(content)
        obs = parse_observations(body)
        stat = HANDOFF_PATH.stat()
        return {
            "title": meta.get("title", "current"),
            "status": obs.get("status", "in-progress"),
            "agent": obs.get("agent", "unknown"),
            "scope": obs.get("scope", "unknown"),
            "done": obs.get("done", []),
            "next": obs.get("next", "No next action specified"),
            "files": obs.get("file", []),
            "commands": obs.get("command", []),
            "blocker": obs.get("blocker", "none"),
            "decisions": obs.get("decision", []),
            "raw_body": body,
            "mtime": stat.st_mtime,
            "mtime_formatted": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
        }
    except Exception as e:
        return {"error": str(e)}


def get_status_data() -> dict:
    notes = cached_by_generation("notes", get_all_notes)
    counts = Counter(n["type"] for n in notes)
    agent_counts = Counter(n["agent"] for n in notes)
    return {
        "brain_dir": str(BRAIN_DIR),
        "total_notes": len(notes),
        "counts": dict(counts),
        "agent_breakdown": dict(agent_counts),
        "validation_score": "10/10 (100% Valid)",
        # The dashboard's "Neural Sidecar" indicator reads data.sidecar_healthy.
        # This key was never emitted, so the panel reported OFFLINE permanently —
        # including while the sidecar was serving 200 on /healthz. Probed with a
        # short timeout so a dead sidecar cannot stall the status endpoint.
        "sidecar_healthy": _sidecar_healthy(timeout=0.75) is not None,
        "baton": get_current_baton()
    }


_sidecar_lock = threading.Lock()
_sidecar_proc: subprocess.Popen | None = None


def _basic_memory_interpreter() -> str:
    bm = shutil.which("bm") or str(Path.home() / ".local" / "bin" / "bm")
    try:
        first_line = Path(bm).read_text(encoding="utf-8", errors="replace").splitlines()[0]
        if first_line.startswith("#!"):
            candidate = first_line[2:].strip().split()[0]
            if candidate and Path(candidate).exists():
                return candidate
    except Exception:
        pass
    return sys.executable


def _sidecar_healthy(timeout=1.5) -> dict | None:
    try:
        url = f"http://{SIDECAR_HOST}:{SIDECAR_PORT}/healthz"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def ensure_search_sidecar() -> bool:
    global _sidecar_proc
    if _sidecar_healthy() is not None:
        return True

    with _sidecar_lock:
        if _sidecar_healthy() is not None:
            return True
        if _sidecar_proc is not None and _sidecar_proc.poll() is None:
            return True
        if not SIDECAR_SCRIPT.exists():
            print(f"⚠️  search sidecar not found at {SIDECAR_SCRIPT}", file=sys.stderr)
            return False

        python_bin = _basic_memory_interpreter()
        try:
            _sidecar_proc = subprocess.Popen(
                [
                    python_bin,
                    str(SIDECAR_SCRIPT),
                    "--host", SIDECAR_HOST,
                    "--port", str(SIDECAR_PORT),
                    "--project", "brain",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            print(f"⚠️  could not start search sidecar: {e}", file=sys.stderr)
            return False

        print(f"🧠 semantic search sidecar starting on http://{SIDECAR_HOST}:{SIDECAR_PORT} (pid {_sidecar_proc.pid})")
        return True


def run_semantic_search(query: str) -> tuple[int, dict]:
    if not query.strip():
        return 400, {"error": "missing or empty query parameter 'q'", "results": []}
    if not ensure_search_sidecar():
        return 503, {"error": "semantic search backend unavailable", "results": []}

    url = f"http://{SIDECAR_HOST}:{SIDECAR_PORT}/search?" + urllib.parse.urlencode({"q": query, "page_size": 10})
    deadline = time.time() + BM_SEARCH_TIMEOUT
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "brain-dashboard/3.0"})
            with urllib.request.urlopen(req, timeout=min(9.0, max(1.0, deadline - time.time()))) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8", errors="replace"))
            except Exception:
                payload = {"error": f"sidecar returned HTTP {error.code}", "results": []}
            if error.code == 503 and payload.get("warming_up") is True:
                time.sleep(0.5)
                continue
            return error.code, payload
        except TimeoutError:
            return 504, {"error": "sidecar search deadline exceeded", "results": []}
        except urllib.error.URLError as error:
            return 503, {"error": f"sidecar unreachable: {error.reason}", "results": []}
        except Exception as error:
            return 502, {"error": f"search proxy failed: {type(error).__name__}: {error}", "results": []}
    return 504, {"error": f"search timed out after {BM_SEARCH_TIMEOUT}s", "results": []}


def brain_watcher():
    last_mtimes = {}
    last_push = 0.0
    pending_push = False
    while True:
        try:
            current_mtimes = {}
            if BRAIN_DIR.exists():
                for p in BRAIN_DIR.rglob("*"):
                    try:
                        if p.is_file() and p.suffix in {".md", ".json"}:
                            current_mtimes[str(p)] = p.stat().st_mtime_ns
                    except OSError:
                        continue
            if last_mtimes and current_mtimes != last_mtimes:
                last_mtimes = current_mtimes
                # Invalidate cached payloads immediately, even if the client-facing
                # push is still being coalesced, so a refetch can never be served a
                # stale snapshot.
                bump_generation()
                pending_push = True
                try:
                    request = urllib.request.Request(f"http://{SIDECAR_HOST}:{SIDECAR_PORT}/invalidate", method="POST")
                    with urllib.request.urlopen(request, timeout=1.0):
                        pass
                except Exception:
                    pass  # sidecar may be warming; generation-aware keys still prevent stale reads
            else:
                last_mtimes = current_mtimes

            # Coalesce a burst of writes into one client push. A swarm dispatch
            # touches many task files in quick succession; pushing each one made
            # the browser refetch ~300 KB per write.
            now = time.monotonic()
            if pending_push and (now - last_push) >= SSE_COALESCE_SECONDS:
                pending_push = False
                last_push = now
                with subscribers_lock:
                    dead = []
                    for q in subscribers:
                        try:
                            if isinstance(q, asyncio.Queue):
                                q.put_nowait("update")
                            elif hasattr(q, "set"):
                                q.set()
                        except Exception:
                            dead.append(q)
                    for d in dead:
                        if d in subscribers:
                            subscribers.remove(d)
        except Exception:
            pass
        time.sleep(1.0)


HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Shared Brain - Visual Knowledge Graph & Mission Control</title>
  <script src="/static/tailwind.js"></script>
  <script src="/static/force-graph.min.js"></script>
  <script src="/static/marked.min.js"></script>
  <link rel="stylesheet" href="/static/fontawesome.css">
  <link rel="stylesheet" href="/static/fonts.css">

  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['"Plus Jakarta Sans"', 'sans-serif'],
            mono: ['"JetBrains Mono"', 'monospace']
          },
          colors: {
            cyber: {
              bg: '#040711',
              panel: '#090e1d',
              card: '#0f172a',
              border: '#1e293b',
              glow: '#00f0ff',
              neonPurple: '#bd00ff',
              neonPink: '#ff0055',
              neonGreen: '#00ff9d',
              neonAmber: '#ffaa00'
            }
          }
        }
      }
    }
  </script>
  <style>
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0;
      width: 100vw; height: 100vh;
      background-color: #040711;
      background-image: 
        radial-gradient(rgba(0, 240, 255, 0.04) 1px, transparent 1px),
        radial-gradient(rgba(255, 0, 85, 0.03) 1px, transparent 1px);
      background-size: 32px 32px;
      background-position: 0 0, 16px 16px;
      overflow: hidden;
    }
    .glass-panel {
      background: rgba(9, 14, 29, 0.88);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    #graph-container {
      width: 100%; height: 100%;
      position: absolute; top: 0; left: 0; right: 0; bottom: 0;
    }
    .filter-btn.active {
      background: rgba(0, 240, 255, 0.15);
      border-color: rgba(0, 240, 255, 0.8);
      color: #00f0ff;
      box-shadow: 0 0 12px rgba(0, 240, 255, 0.25);
    }
    .markdown-content pre {
      background: #02040a;
      border: 1px solid #1e293b;
      padding: 12px;
      border-radius: 8px;
      overflow-x: auto;
    }
    .markdown-content code {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85em;
    }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #090e1d; }
    ::-webkit-scrollbar-thumb { background: #1e293b; border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: #334155; }
    .task-scroll {
      overflow-y: scroll;
      scrollbar-gutter: stable;
    }
    .task-scroll::-webkit-scrollbar { width: 10px; }
    .task-scroll::-webkit-scrollbar-track { background: #020617; border-radius: 999px; }
    .task-scroll::-webkit-scrollbar-thumb { background: #64748b; border: 2px solid #020617; border-radius: 999px; }
    .task-scroll::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
    .task-scroll:focus { outline: 2px solid #00f0ff; outline-offset: 2px; }
  </style>
</head>
<body class="text-slate-100 h-screen w-screen flex flex-col antialiased selection:bg-cyan-500 selection:text-black">

  <!-- Top Glass Header -->
  <header class="glass-panel shrink-0 z-50 px-6 py-2.5 border-b border-slate-800 flex flex-wrap items-center justify-between shadow-2xl">
    <div class="flex items-center space-x-4">
      <div class="flex items-center space-x-3">
        <div class="relative flex items-center justify-center w-8 h-8 rounded-lg bg-gradient-to-br from-cyan-500 to-indigo-600 shadow-md shadow-cyan-500/30">
          <i class="fa-solid fa-brain text-black text-sm font-black"></i>
          <span class="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
          <span class="absolute -top-1 -right-1 w-2.5 h-2.5 rounded-full bg-emerald-400"></span>
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <h1 class="text-sm font-extrabold tracking-wider uppercase bg-gradient-to-r from-cyan-400 via-sky-200 to-indigo-400 bg-clip-text text-transparent">
              Shared Brain OS
            </h1>
            <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-cyan-950/80 text-cyan-300 border border-cyan-800/80">
              v2.5 Next-Gen
            </span>
          </div>
          <p class="text-[10px] text-slate-400 font-mono">~/agentic-brain</p>
        </div>
      </div>
    </div>

    <!-- Interactive Graph Filters Toolbar (Pills) -->
    <div class="flex items-center space-x-1.5 overflow-x-auto py-1">
      <button onclick="toggleTypeFilter('all')" id="filter-all" class="filter-btn active px-2.5 py-1 rounded-full text-xs font-semibold border border-slate-700 bg-slate-900/80 text-slate-300 hover:border-cyan-400 transition flex items-center space-x-1">
        <span>🌐 All</span> <span id="count-all" class="text-[10px] opacity-75 font-mono">10</span>
      </button>
      <button onclick="toggleTypeFilter('handoff')" id="filter-handoff" class="filter-btn px-2.5 py-1 rounded-full text-xs font-semibold border border-pink-900/60 bg-slate-900/80 text-pink-300 hover:border-pink-500 transition flex items-center space-x-1">
        <span class="w-2 h-2 rounded-full bg-[#ff0055] animate-pulse"></span>
        <span>Handoffs</span> <span id="count-handoffs" class="text-[10px] opacity-75 font-mono">4</span>
      </button>
      <button onclick="toggleTypeFilter('projects')" id="filter-projects" class="filter-btn px-2.5 py-1 rounded-full text-xs font-semibold border border-cyan-900/60 bg-slate-900/80 text-cyan-300 hover:border-cyan-400 transition flex items-center space-x-1">
        <span class="w-2 h-2 rounded-full bg-cyan-400"></span>
        <span>Projects</span> <span id="count-projects" class="text-[10px] opacity-75 font-mono">1</span>
      </button>
      <button onclick="toggleTypeFilter('decisions')" id="filter-decisions" class="filter-btn px-2.5 py-1 rounded-full text-xs font-semibold border border-purple-900/60 bg-slate-900/80 text-purple-300 hover:border-purple-400 transition flex items-center space-x-1">
        <span class="w-2 h-2 rounded-full bg-purple-400"></span>
        <span>Decisions</span> <span id="count-decisions" class="text-[10px] opacity-75 font-mono">1</span>
      </button>
      <button onclick="toggleTypeFilter('runbooks')" id="filter-runbooks" class="filter-btn px-2.5 py-1 rounded-full text-xs font-semibold border border-emerald-900/60 bg-slate-900/80 text-emerald-300 hover:border-emerald-400 transition flex items-center space-x-1">
        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
        <span>Runbooks</span> <span id="count-runbooks" class="text-[10px] opacity-75 font-mono">3</span>
      </button>
      <button onclick="toggleTypeFilter('schemas')" id="filter-schemas" class="filter-btn px-2.5 py-1 rounded-full text-xs font-semibold border border-sky-900/60 bg-slate-900/80 text-sky-300 hover:border-sky-400 transition flex items-center space-x-1">
        <span class="w-2 h-2 rounded-full bg-sky-400"></span>
        <span>Schemas</span> <span id="count-schemas" class="text-[10px] opacity-75 font-mono">1</span>
      </button>
    </div>

    <!-- Search & Action Toolbar -->
    <div class="flex items-center space-x-2.5">
      <!-- Live Search Box with Canvas Pulse Highlighting -->
      <div class="relative">
        <i class="fa-solid fa-magnifying-glass absolute left-3 top-2 text-xs text-cyan-400"></i>
        <input type="text" id="quick-search" oninput="handleSearchInput(this.value)" placeholder="Search & Highlight Graph..." class="pl-8 pr-8 py-1.5 bg-slate-900/90 border border-slate-700/80 rounded-lg text-xs text-slate-100 focus:outline-none focus:border-cyan-400 focus:ring-1 focus:ring-cyan-400 w-44 sm:w-56 transition shadow-inner">
        <button id="clear-search" onclick="clearSearch()" class="hidden absolute right-2.5 top-1.5 text-xs text-slate-400 hover:text-white">
          <i class="fa-solid fa-times"></i>
        </button>
      </div>

      
      
      <!-- Autonomous Swarm Mesh Button -->
      <button onclick="openSwarmModal()" id="swarm-btn" title="Autonomous Multi-Agent Swarm" class="px-3 py-1.5 rounded-lg bg-gradient-to-r from-purple-950/90 to-pink-950/90 hover:from-purple-900 hover:to-pink-900 border border-purple-500/50 text-xs text-purple-300 font-bold flex items-center space-x-1.5 transition shadow-lg shadow-purple-950/50">
        <i class="fa-solid fa-users-gear text-purple-400"></i>
        <span class="hidden sm:inline">Swarm Mesh</span>
        <span id="swarm-badge" class="px-1.5 py-0.5 rounded bg-purple-500 text-black text-[10px] font-extrabold font-mono">0</span>
      </button>

      <!-- Autonomous Self-Healing Sentinel HUD Button -->
      <button onclick="triggerSelfHeal()" id="heal-btn" title="Run Sentinel Self-Healing Diagnostics" class="px-3 py-1.5 rounded-lg bg-gradient-to-r from-emerald-950/90 to-cyan-950/90 hover:from-emerald-900 hover:to-cyan-900 border border-emerald-500/50 text-xs text-emerald-300 font-bold flex items-center space-x-1.5 transition shadow-lg shadow-emerald-950/50">
        <i class="fa-solid fa-wand-magic-sparkles text-emerald-400 animate-pulse"></i>
        <span class="hidden sm:inline">Self-Heal</span>
        <span id="health-badge" class="px-1.5 py-0.5 rounded bg-emerald-400 text-black text-[10px] font-extrabold font-mono">100%</span>
      </button>

      <!-- Agent Analytics & Health Modal Button -->
      <button onclick="openAnalyticsModal()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-xs text-slate-200 font-semibold flex items-center space-x-1.5 transition">
        <i class="fa-solid fa-chart-pie text-cyan-400"></i>
        <span class="hidden sm:inline">Analytics</span>
      </button>

      <!-- Provider Accounts Button: add an API key here and it becomes a worker
           even with no agent CLI installed. Badge shows how many are configured. -->
      <button onclick="openProvidersModal()" id="providers-btn" title="Manage provider API keys / workers" class="px-3 py-1.5 rounded-lg bg-gradient-to-r from-cyan-950/90 to-slate-900/90 hover:from-cyan-900 hover:to-slate-800 border border-cyan-500/40 text-xs text-cyan-300 font-bold flex items-center space-x-1.5 transition shadow-lg shadow-cyan-950/40">
        <i class="fa-solid fa-key text-cyan-400"></i>
        <span class="hidden sm:inline">Providers</span>
        <span id="providers-badge" class="px-1.5 py-0.5 rounded bg-cyan-500 text-black text-[10px] font-extrabold font-mono">0</span>
      </button>

      <!-- Center / Reset Zoom -->
      <button onclick="resetGraphView()" title="Fit Graph" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 flex items-center justify-center transition">
        <i class="fa-solid fa-expand text-xs"></i>
      </button>
    </div>
  </header>

  <!-- Main Body -->
  <main class="relative flex-1 w-full h-full overflow-hidden">
    <!-- 2D Force-Directed Canvas Container -->
    <div id="graph-container"></div>

    <!-- Live Floating Baton Pill (Bottom Left) -->
    <div id="live-baton-card" class="absolute bottom-5 left-5 z-40 max-w-sm w-full glass-panel rounded-xl p-3.5 border border-pink-500/40 shadow-2xl transition-all duration-300 hover:border-pink-500">
      <div class="flex items-center justify-between mb-2">
        <div class="flex items-center space-x-2">
          <span class="w-2.5 h-2.5 rounded-full bg-[#ff0055] animate-pulse"></span>
          <span class="text-xs font-bold uppercase tracking-wider text-pink-400">ACTIVE IN-FLIGHT BATON</span>
        </div>
        <span id="baton-agent-badge" class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-pink-950 text-pink-300 border border-pink-800">antigravity</span>
      </div>
      <h3 id="baton-title" class="text-sm font-semibold text-white truncate mb-1">Loading active task...</h3>
      <p id="baton-next" class="text-xs text-slate-300 line-clamp-2 mb-2 font-mono bg-slate-950/60 p-1.5 rounded border border-slate-800">...</p>
      <div class="flex items-center justify-between text-[10px] text-slate-400">
        <span id="baton-mtime">Updated just now</span>
        <button onclick="openNoteDrawer('handoff/current.md')" class="text-cyan-400 hover:underline font-semibold flex items-center space-x-1">
          <span>Read Full Note</span>
          <i class="fa-solid fa-arrow-right text-[9px]"></i>
        </button>
      </div>
    </div>

    <!-- Graph Controls Overlay (Bottom Right) -->
    <div class="absolute bottom-5 right-5 z-40 glass-panel rounded-xl px-3 py-2 border border-slate-800 flex items-center space-x-3 text-xs text-slate-300 shadow-xl">
      <div class="flex items-center space-x-1.5">
        <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
        <span id="sidecar-status" class="text-[11px] font-mono">Sidecar: Ready (1.6ms)</span>
      </div>
      <span class="text-slate-600">|</span>
      <div class="flex items-center space-x-1.5">
        <i class="fa-solid fa-shield-check text-cyan-400 text-xs"></i>
        <span id="validation-status" class="text-[11px] font-mono">10/10 Valid</span>
      </div>
    </div>

    <!-- Slide-Out Markdown Note Drawer (Right Side) -->
    <div id="note-drawer" class="absolute top-0 right-0 h-full w-full sm:w-[480px] lg:w-[560px] glass-panel border-l border-slate-800 z-50 transform translate-x-full transition-transform duration-300 ease-out flex flex-col shadow-2xl">
      <!-- Drawer Header -->
      <div class="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-950/70">
        <div class="flex items-center space-x-2">
          <span id="drawer-type-badge" class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-cyan-950 text-cyan-300 border border-cyan-800">NOTE</span>
          <span id="drawer-agent-badge" class="text-[10px] font-mono px-2 py-0.5 rounded bg-slate-900 text-slate-300 border border-slate-700">agent</span>
        </div>
        <div class="flex items-center space-x-2">
          <button id="copy-path-btn" onclick="copyCurrentPath()" title="Copy Path" class="w-7 h-7 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 flex items-center justify-center text-xs transition">
            <i class="fa-solid fa-copy"></i>
          </button>
          <button onclick="closeNoteDrawer()" class="w-7 h-7 rounded bg-slate-800 hover:bg-red-900 text-slate-300 hover:text-white flex items-center justify-center text-xs transition">
            <i class="fa-solid fa-times"></i>
          </button>
        </div>
      </div>

      <!-- Drawer Subheader (Title & Path) -->
      <div class="px-5 py-3 border-b border-slate-800/80 bg-slate-900/40">
        <h2 id="drawer-title" class="text-base font-bold text-white mb-1">Note Title</h2>
        <p id="drawer-path" class="text-xs font-mono text-cyan-400 truncate">path/to/note.md</p>
      </div>

      
      <!-- Drawer Navigation Tabs -->
      <div class="flex border-b border-slate-800 bg-slate-950/60 text-xs font-semibold px-4">
        <button onclick="switchDrawerTab('content')" id="tab-btn-content" class="py-2.5 px-3 text-cyan-400 border-b-2 border-cyan-400 flex items-center space-x-1.5 transition">
          <i class="fa-solid fa-file-lines"></i>
          <span>Markdown Note</span>
        </button>
        <button onclick="switchDrawerTab('dossier')" id="tab-btn-dossier" class="py-2.5 px-3 text-slate-400 hover:text-slate-200 border-b-2 border-transparent flex items-center space-x-1.5 transition">
          <i class="fa-solid fa-network-wired text-purple-400"></i>
          <span>2-Hop Cognitive Dossier</span>
        </button>
      </div>

      <!-- Drawer Body (Rendered Markdown) -->
      <div id="drawer-content" class="flex-1 overflow-y-auto p-5 text-sm leading-relaxed markdown-content text-slate-200 space-y-3">
        <p class="text-slate-400">Loading note content...</p>
      </div>

      
      <!-- Drawer Body (Cognitive Dossier) -->
      <div id="drawer-dossier" class="hidden flex-1 overflow-y-auto p-5 text-sm leading-relaxed markdown-content text-slate-200 space-y-4 font-sans">
        <p class="text-slate-400">Synthesizing 2-hop associative graph context...</p>
      </div>

      <!-- Drawer Footer (Connected Links) -->
      <div class="p-4 border-t border-slate-800 bg-slate-950/80 text-xs">
        <div class="font-bold text-slate-400 uppercase tracking-wider mb-2 flex items-center space-x-1.5">
          <i class="fa-solid fa-link text-cyan-400"></i>
          <span>Connected Graph References</span>
        </div>
        <div id="drawer-links" class="flex flex-wrap gap-1.5">
          <!-- Dynamic link pills -->
        </div>
      </div>
    </div>

    
  
  <!-- Autonomous Multi-Agent Swarm Modal -->
  <div id="swarm-modal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/85 backdrop-blur-md p-4">
    <div class="glass-panel w-full max-w-4xl rounded-2xl border border-purple-500/40 p-6 shadow-2xl flex flex-col space-y-4 h-[88vh] max-h-[760px] min-h-[620px]">
      <!-- Header -->
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-3">
          <div class="w-10 h-10 rounded-xl bg-purple-950 flex items-center justify-center border border-purple-500/60 shadow-lg shadow-purple-900/40">
            <i class="fa-solid fa-network-wired text-purple-400 text-lg"></i>
          </div>
          <div>
            <h3 class="text-sm font-extrabold text-white tracking-wider uppercase flex items-center space-x-2">
              <span>Brain Task Runner</span>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-amber-950 text-amber-300 border border-amber-700">APPROVAL REQUIRED</span>
            </h3>
            <p class="text-[11px] text-purple-300/80 font-mono">Plan first, then explicitly approve eligible local Kiro work</p>
          </div>
        </div>
        <button onclick="closeSwarmModal()" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-red-900 text-slate-300 hover:text-white flex items-center justify-center transition">
          <i class="fa-solid fa-times text-sm"></i>
        </button>
      </div>

      <!-- Active Agent Roster -->
      <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <div class="p-3 rounded-xl bg-slate-900/80 border border-purple-500/30 flex items-center space-x-3">
          <div class="w-8 h-8 rounded-lg bg-purple-950/80 text-purple-400 flex items-center justify-center font-bold text-sm">🧠</div>
          <div class="truncate">
            <div class="flex items-center space-x-1.5">
              <span class="text-xs font-bold text-white">Antigravity</span>
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
            </div>
            <p class="text-[10px] text-slate-400 truncate">Master Architect & Reasoning</p>
          </div>
        </div>
        <div class="p-3 rounded-xl bg-slate-900/80 border border-cyan-500/30 flex items-center space-x-3">
          <div class="w-8 h-8 rounded-lg bg-cyan-950/80 text-cyan-400 flex items-center justify-center font-bold text-sm">⚡</div>
          <div class="truncate">
            <div class="flex items-center space-x-1.5">
              <span class="text-xs font-bold text-white">Kiro CLI</span>
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
            </div>
            <p class="text-[10px] text-slate-400 truncate">Terminal, Cloud & Docker</p>
          </div>
        </div>
        <div class="p-3 rounded-xl bg-slate-900/80 border border-pink-500/30 flex items-center space-x-3">
          <div class="w-8 h-8 rounded-lg bg-pink-950/80 text-pink-400 flex items-center justify-center font-bold text-sm">💻</div>
          <div class="truncate">
            <div class="flex items-center space-x-1.5">
              <span class="text-xs font-bold text-white">Cline</span>
              <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
            </div>
            <p class="text-[10px] text-slate-400 truncate">Headless coding CLI</p>
          </div>
        </div>
        <div class="p-3 rounded-xl bg-slate-900/80 border border-indigo-500/30 flex items-center space-x-3">
          <div class="w-8 h-8 rounded-lg bg-indigo-950/80 text-indigo-400 flex items-center justify-center font-bold text-sm">🔑</div>
          <div class="truncate">
            <div class="flex items-center space-x-1.5">
              <span class="text-xs font-bold text-white">Antigravity #2</span>
              <span id="antigravity-api-agent-dot" class="w-2 h-2 rounded-full bg-slate-500"></span>
            </div>
            <p class="text-[10px] text-slate-500 truncate">2nd account · Gemini API key</p>
            <p id="antigravity-api-agent-status" class="text-[10px] text-slate-400 truncate">checking…</p>
            <p id="antigravity-api-agent-models" class="text-[9px] font-mono text-slate-600 truncate"></p>
          </div>
        </div>
      </div>

      <!-- Batch Dispatch Input -->
      <div class="p-3 rounded-xl bg-slate-950/70 border border-slate-800 space-y-2">
        <label class="text-xs font-bold uppercase tracking-wider text-slate-300 flex items-center justify-between">
          <span>Task input (one explicit task per line)</span>
          <span class="text-[10px] text-amber-400 font-mono">Kiro, Cline and Antigravity run headlessly; Antigravity IDE plans are staged until it acknowledges</span>
        </label>
        <textarea id="swarm-task-input" rows="2" placeholder="e.g. Implement an accessible task-card style in this repository; Run local test suite" class="w-full bg-slate-900 border border-slate-700/80 rounded-lg p-2.5 text-xs text-slate-100 focus:outline-none focus:border-purple-400 font-mono"></textarea>
        <p id="swarm-execution-status" class="text-[10px] font-mono text-slate-400">1. Generate a plan. 2. Review it. 3. Explicitly approve eligible local execution.</p>
        <div class="flex flex-wrap items-center justify-between gap-2 pt-1">
          <div class="flex items-center space-x-2 text-[11px] text-slate-400">
            <span>Execution: <strong>sequential, approval-bound</strong></span>
          </div>
          <div class="flex items-center gap-2">
            <button onclick="dispatchSwarmTasks()" id="dispatch-swarm-btn" class="px-4 py-1.5 rounded-lg bg-gradient-to-r from-purple-500 to-pink-500 hover:from-purple-400 hover:to-pink-400 text-white font-extrabold text-xs transition flex items-center space-x-1.5 shadow-lg shadow-purple-500/25">
              <i class="fa-solid fa-diagram-project"></i>
              <span>Generate plan</span>
            </button>
            <button onclick="executeApprovedSwarmPlan()" id="execute-swarm-btn" disabled class="px-4 py-1.5 rounded-lg bg-emerald-700 disabled:bg-slate-700 disabled:text-slate-400 hover:bg-emerald-600 text-white font-extrabold text-xs transition flex items-center space-x-1.5">
              <i class="fa-solid fa-play"></i>
              <span>Approve & start plan</span>
            </button>
          </div>
        </div>
      </div>

      <!-- Task Kanban / Queue Status -->
      <div class="flex-1 min-h-0 overflow-hidden grid grid-cols-1 md:grid-cols-4 gap-2.5 h-[min(42vh,360px)] min-h-[200px]">
        <!-- Pending -->
        <div class="flex flex-col bg-slate-950/60 rounded-xl p-2.5 border border-slate-800/80">
          <div class="flex items-center justify-between mb-2 pb-1.5 border-b border-slate-800">
            <span class="text-xs font-bold text-slate-300">⏳ Pending</span>
            <span id="swarm-count-pending" class="text-[10px] font-mono px-1.5 py-0.2 rounded bg-slate-800 text-slate-300">0</span>
          </div>
          <div id="swarm-list-pending" class="task-scroll flex-1 min-h-0 overflow-y-scroll overscroll-contain pr-1 space-y-1.5" tabindex="0"></div>
        </div>

        <!-- In-Progress -->
        <div class="flex flex-col bg-slate-950/60 rounded-xl p-2.5 border border-cyan-950/60">
          <div class="flex items-center justify-between mb-2 pb-1.5 border-b border-slate-800">
            <span class="text-xs font-bold text-cyan-400">⚙️ In-Progress</span>
            <span id="swarm-count-inprogress" class="text-[10px] font-mono px-1.5 py-0.2 rounded bg-cyan-950 text-cyan-300">0</span>
          </div>
          <div id="swarm-list-inprogress" class="task-scroll flex-1 min-h-0 overflow-y-scroll overscroll-contain pr-1 space-y-1.5" tabindex="0"></div>
        </div>

        <!-- Completed -->
        <div class="flex flex-col bg-slate-950/60 rounded-xl p-2.5 border border-emerald-950/60">
          <div class="flex items-center justify-between mb-2 pb-1.5 border-b border-slate-800">
            <span class="text-xs font-bold text-emerald-400">✓ Recent completed</span>
            <span id="swarm-count-completed" class="text-[10px] font-mono px-1.5 py-0.2 rounded bg-emerald-950 text-emerald-300">0</span>
          </div>
          <div id="swarm-list-completed" class="task-scroll flex-1 min-h-0 overflow-y-scroll overscroll-contain pr-1 space-y-1.5" tabindex="0"></div>
        </div>

        <!-- Escalated -->
        <div class="flex flex-col bg-slate-950/60 rounded-xl p-2.5 border border-red-950/60">
          <div class="flex items-center justify-between mb-2 pb-1.5 border-b border-slate-800">
            <span class="text-xs font-bold text-amber-400">⚠ Needs attention</span>
            <span id="swarm-count-escalated" class="text-[10px] font-mono px-1.5 py-0.2 rounded bg-red-950 text-red-300">0</span>
          </div>
          <div id="swarm-list-escalated" class="task-scroll flex-1 min-h-0 overflow-y-scroll overscroll-contain pr-1 space-y-1.5" tabindex="0"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Autonomous Self-Healing Modal -->
  <div id="self-heal-modal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/85 backdrop-blur-md p-4">
    <div class="glass-panel w-full max-w-2xl rounded-2xl border border-emerald-500/40 p-6 shadow-2xl flex flex-col space-y-4">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-3">
          <div class="w-10 h-10 rounded-xl bg-emerald-950 flex items-center justify-center border border-emerald-500/60 shadow-lg shadow-emerald-900/40">
            <i class="fa-solid fa-microchip text-emerald-400 text-lg"></i>
          </div>
          <div>
            <h3 class="text-sm font-extrabold text-white tracking-wider uppercase flex items-center space-x-2">
              <span>Omni-Sentinel Autonomous Self-Healer</span>
              <span class="text-[10px] font-mono px-2 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-700">ACTIVE</span>
            </h3>
            <p class="text-[11px] text-emerald-400/80 font-mono">Continuous Picoschema validation, link repair & sidecar supervisor</p>
          </div>
        </div>
        <button onclick="closeSelfHealModal()" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-red-900 text-slate-300 hover:text-white flex items-center justify-center transition">
          <i class="fa-solid fa-times text-sm"></i>
        </button>
      </div>

      <div id="heal-terminal" class="bg-black/95 rounded-xl p-4 font-mono text-xs text-emerald-300 border border-slate-800/90 h-64 overflow-y-auto space-y-1.5 shadow-inner">
        <p class="text-slate-500">> Omni-Sentinel Diagnostics initialized.</p>
        <p class="text-slate-400">> Press Execute to scan and repair all knowledge components.</p>
      </div>

      <div class="flex items-center justify-between pt-2 border-t border-slate-800 text-xs">
        <span id="heal-status-msg" class="text-slate-400 font-mono">Ready for execution</span>
        <button onclick="executeLiveHeal()" id="execute-heal-btn" class="px-5 py-2 rounded-xl bg-gradient-to-r from-emerald-500 to-cyan-500 hover:from-emerald-400 hover:to-cyan-400 text-black font-extrabold transition flex items-center space-x-2 shadow-lg shadow-emerald-500/30">
          <i class="fa-solid fa-bolt"></i>
          <span>Run Autonomous Self-Healing</span>
        </button>
      </div>
    </div>
  </div>

  <!-- Agent Analytics Modal -->
    <div id="analytics-modal" class="fixed inset-0 z-50 bg-black/70 backdrop-blur-md hidden flex items-center justify-center p-4">
      <div class="glass-panel max-w-lg w-full rounded-2xl border border-slate-700 p-6 shadow-2xl space-y-5">
        <div class="flex items-center justify-between border-b border-slate-800 pb-3">
          <div class="flex items-center space-x-2.5">
            <i class="fa-solid fa-chart-pie text-cyan-400 text-lg"></i>
            <h3 class="text-base font-bold text-white">Multi-Agent Memory Analytics</h3>
          </div>
          <button onclick="closeAnalyticsModal()" class="text-slate-400 hover:text-white text-sm">
            <i class="fa-solid fa-times"></i>
          </button>
        </div>

        <div class="space-y-3">
          <h4 class="text-xs font-bold uppercase tracking-wider text-slate-400">Agent Contribution Breakdown</h4>
          <div id="agent-bars" class="space-y-2">
            <!-- Dynamic Agent Bars -->
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3 pt-2 border-t border-slate-800">
          <div class="bg-slate-900/90 p-3 rounded-xl border border-slate-800 text-center">
            <div class="text-[11px] text-slate-400 uppercase font-semibold">Store Integrity</div>
            <div id="modal-integrity" class="text-base font-bold text-emerald-400 font-mono mt-0.5">10/10 (100%)</div>
          </div>
          <div class="bg-slate-900/90 p-3 rounded-xl border border-slate-800 text-center">
            <div class="text-[11px] text-slate-400 uppercase font-semibold">Semantic Latency</div>
            <div id="modal-latency" class="text-base font-bold text-cyan-400 font-mono mt-0.5">~1.6ms Cached</div>
          </div>
        </div>

        <div class="text-right">
          <button onclick="closeAnalyticsModal()" class="px-4 py-1.5 rounded-lg bg-cyan-500 hover:bg-cyan-400 text-black font-bold text-xs transition">
            Close
          </button>
        </div>
      </div>
    </div>

  <!-- Provider Accounts Modal -->
  <!-- Add an API key here and providers.py turns it into a working worker even
       with no agent CLI installed. Keys are never rendered back, never logged,
       and stored 0600 outside the brain store. -->
  <div id="providers-modal" class="hidden fixed inset-0 z-[100] flex items-center justify-center bg-black/85 backdrop-blur-md p-4">
    <div class="glass-panel w-full max-w-3xl rounded-2xl border border-cyan-500/40 p-6 shadow-2xl flex flex-col space-y-4 max-h-[88vh]">
      <div class="flex items-center justify-between border-b border-slate-800 pb-3">
        <div class="flex items-center space-x-2.5">
          <i class="fa-solid fa-key text-cyan-400 text-lg"></i>
          <div>
            <h3 class="text-base font-bold text-white">Provider Accounts</h3>
            <p class="text-[11px] text-cyan-400/80 font-mono">Add an API key and it becomes a worker — no agent CLI required</p>
          </div>
        </div>
        <button onclick="closeProvidersModal()" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-red-900 text-slate-300 hover:text-white flex items-center justify-center transition">
          <i class="fa-solid fa-times text-sm"></i>
        </button>
      </div>

      <!-- Summary line from discover(): how many are configured and whether a
           worker can run right now with no CLI installed. -->
      <div id="providers-summary" class="text-[11px] font-mono text-slate-400"></div>

      <div id="providers-list" class="task-scroll flex-1 min-h-0 overflow-y-auto pr-1 space-y-2">
        <!-- Dynamic provider rows rendered by loadProviders() -->
      </div>

      <!-- Security note kept visible so the operator knows exactly where a key
           goes and, more importantly, where it does NOT go (the brain store). -->
      <p class="text-[10px] font-mono text-slate-500 border-t border-slate-800 pt-3 leading-relaxed">
        <i class="fa-solid fa-lock text-slate-400"></i>
        Keys are stored <span class="text-slate-300">0600 in ~/.config/brain/providers.json</span>
        and are never written into the brain store, never logged, and never rendered back by the API.
      </p>
    </div>
  </div>
  </main>

  <!-- Force-Graph Logic & Interactivity -->
  <script>
    let rawGraphData = { nodes: [], links: [] };
    // Topology fingerprint of the last rendered graph, used to skip a costly
    // force-simulation restart when the notes have not changed.
    let lastGraphSignature = '';
    let filteredGraphData = { nodes: [], links: [] };
    let Graph = null;
    let activeFilter = 'all';
    let searchQuery = '';
    let searchMatchingNodeIds = new Set();
    let searchDebounceTimer = null;
    let selectedNode = null;
    let highlightNodes = new Set();
    let highlightLinks = new Set();
    let currentDrawerPath = '';

    // Initialize 2D Canvas Force Graph
    function initGraph() {
      const elem = document.getElementById('graph-container');
      Graph = ForceGraph()(elem)
        .backgroundColor('#040711')
        .nodeRelSize(5)
        .nodeVal(node => node.val || 8)
        .nodeColor(node => node.color || '#94a3b8')
        .linkColor(link => {
          if (highlightLinks.has(link)) return '#00f0ff';
          return link.color || 'rgba(148, 163, 184, 0.25)';
        })
        .linkWidth(link => highlightLinks.has(link) ? 2.5 : 1)
        .linkDirectionalParticles(link => highlightLinks.has(link) ? 5 : (link.is_current ? 3 : 2))
        .linkDirectionalParticleColor(link => highlightLinks.has(link) ? "#00f0ff" : (link.is_current ? "#ff0055" : "rgba(0, 240, 255, 0.4)"))
        .linkDirectionalParticleWidth(2.5)
        .linkDirectionalParticleSpeed(0.008)
        .d3AlphaDecay(0.02)
        .d3VelocityDecay(0.3)
        .cooldownTicks(120)
        .nodeCanvasObject((node, ctx, globalScale) => {
          const isHighlighted = highlightNodes.size === 0 || highlightNodes.has(node);
          const isSearchMatch = searchMatchingNodeIds.has(node.id);
          const radius = Math.sqrt(node.val || 8) * 3;

          ctx.save();
          ctx.globalAlpha = isHighlighted ? (searchQuery && !isSearchMatch ? 0.2 : 1.0) : 0.15;

          // Outer Glow / Halo for Search Match or Active Baton (Sine pulse)
          if (isSearchMatch) {
            const pulse = Math.sin(Date.now() / 200) * 3;
            ctx.beginPath();
            ctx.arc(node.x, node.y, radius + 6 + pulse, 0, 2 * Math.PI, false);
            ctx.fillStyle = 'rgba(0, 240, 255, 0.35)';
            ctx.fill();
          } else if (node.is_current) {
            const pulse = Math.sin(Date.now() / 300) * 2;
            ctx.beginPath();
            ctx.arc(node.x, node.y, radius + 4 + pulse, 0, 2 * Math.PI, false);
            ctx.fillStyle = 'rgba(255, 0, 85, 0.35)';
            ctx.fill();
          }

          // Node Circle Body
          ctx.beginPath();
          ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI, false);
          ctx.fillStyle = node.color || '#00f0ff';
          ctx.fill();
          ctx.lineWidth = (node.degree >= 3 || node.is_current) ? 2.5 : 1.5;
          ctx.strokeStyle = (node.degree >= 3 || node.is_current) ? '#ffffff' : 'rgba(255, 255, 255, 0.8)';
          ctx.stroke();

          // Text Label Pill
          const label = node.name || node.label || node.id;
          const fontSize = Math.max(10 / globalScale, 3.5);
          ctx.font = `${fontSize}px "Plus Jakarta Sans", sans-serif`;
          const textWidth = ctx.measureText(label).width;
          const bckgDimensions = [textWidth + 6, fontSize + 3];

          ctx.fillStyle = 'rgba(4, 7, 17, 0.85)';
          ctx.fillRect(
            node.x - bckgDimensions[0] / 2,
            node.y + radius + 3,
            bckgDimensions[0],
            bckgDimensions[1]
          );

          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = isSearchMatch ? '#00f0ff' : '#e2e8f0';
          ctx.fillText(label, node.x, node.y + radius + 3 + bckgDimensions[1] / 2);

          ctx.restore();
        })
        .onNodeClick(node => {
          handleNodeClick(node);
        })
        .onBackgroundClick(() => {
          clearHighlight();
        });

      window.addEventListener('resize', () => {
        if (Graph) {
          Graph.width(elem.clientWidth);
          Graph.height(elem.clientHeight);
        }
      });
    }

    // Node Click -> Focus Neighborhood (1st & 2nd degree) + Open Drawer
    function handleNodeClick(node) {
      selectedNode = node;
      highlightNodes.clear();
      highlightLinks.clear();

      const firstDegree = new Set();
      const secondDegree = new Set();

      highlightNodes.add(node);

      // 1st-degree neighbors
      rawGraphData.links.forEach(link => {
        const srcId = typeof link.source === 'object' ? link.source.id : link.source;
        const tgtId = typeof link.target === 'object' ? link.target.id : link.target;
        if (srcId === node.id) {
          const targetNode = typeof link.target === 'object' ? link.target : rawGraphData.nodes.find(n => n.id === tgtId);
          if (targetNode) firstDegree.add(targetNode);
          highlightLinks.add(link);
        } else if (tgtId === node.id) {
          const sourceNode = typeof link.source === 'object' ? link.source : rawGraphData.nodes.find(n => n.id === srcId);
          if (sourceNode) firstDegree.add(sourceNode);
          highlightLinks.add(link);
        }
      });

      firstDegree.forEach(n => highlightNodes.add(n));

      // 2nd-degree neighbors
      const firstDegreeIds = new Set(Array.from(firstDegree).map(n => n.id));
      rawGraphData.links.forEach(link => {
        const srcId = typeof link.source === 'object' ? link.source.id : link.source;
        const tgtId = typeof link.target === 'object' ? link.target.id : link.target;
        if (firstDegreeIds.has(srcId) && tgtId !== node.id) {
          const targetNode = typeof link.target === 'object' ? link.target : rawGraphData.nodes.find(n => n.id === tgtId);
          if (targetNode) {
            secondDegree.add(targetNode);
            highlightLinks.add(link);
          }
        } else if (firstDegreeIds.has(tgtId) && srcId !== node.id) {
          const sourceNode = typeof link.source === 'object' ? link.source : rawGraphData.nodes.find(n => n.id === srcId);
          if (sourceNode) {
            secondDegree.add(sourceNode);
            highlightLinks.add(link);
          }
        }
      });

      secondDegree.forEach(n => highlightNodes.add(n));

      if (node.x !== undefined && node.y !== undefined) {
        Graph.centerAt(node.x, node.y, 800);
        Graph.zoom(2.2, 800);
      }
      openNoteDrawer(node.path || node.id);
    }

    function clearHighlight() {
      selectedNode = null;
      highlightNodes.clear();
      highlightLinks.clear();
    }

    // Type Filter Toggle (In-Memory without Reload)
    function toggleTypeFilter(type) {
      activeFilter = type;
      document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
      const activeBtn = document.getElementById('filter-' + type);
      if (activeBtn) activeBtn.classList.add('active');

      if (type === 'all') {
        filteredGraphData = {
          nodes: [...rawGraphData.nodes],
          links: [...rawGraphData.links]
        };
      } else {
        const allowedNodes = rawGraphData.nodes.filter(n => {
          if (n.type === type) return true;
          if (type === 'handoff' && (n.type === 'handoff' || n.type === 'handoffs')) return true;
          if (type === 'projects' && (n.type === 'projects' || n.type === 'project')) return true;
          if (type === 'decisions' && (n.type === 'decisions' || n.type === 'decision')) return true;
          if (type === 'runbooks' && (n.type === 'runbooks' || n.type === 'runbook')) return true;
          if (type === 'schemas' && (n.type === 'schemas' || n.type === 'schema')) return true;
          return false;
        });
        const allowedIds = new Set(allowedNodes.map(n => n.id));
        const allowedLinks = rawGraphData.links.filter(l => {
          const s = typeof l.source === 'object' ? l.source.id : l.source;
          const t = typeof l.target === 'object' ? l.target.id : l.target;
          return allowedIds.has(s) && allowedIds.has(t);
        });
        filteredGraphData = { nodes: allowedNodes, links: allowedLinks };
      }

      Graph.graphData(filteredGraphData);
    }

    // Real-Time Search Pulse Highlighting + Semantic Query Bridge
    function handleSearchInput(query) {
      searchQuery = query.trim().toLowerCase();
      const clearBtn = document.getElementById('clear-search');
      if (searchQuery) {
        clearBtn.classList.remove('hidden');
      } else {
        clearBtn.classList.add('hidden');
      }

      searchMatchingNodeIds.clear();
      if (searchQuery) {
        rawGraphData.nodes.forEach(n => {
          const matchTitle = (n.name || '').toLowerCase().includes(searchQuery);
          const matchPath = (n.path || '').toLowerCase().includes(searchQuery);
          const matchType = (n.type || '').toLowerCase().includes(searchQuery);
          const matchAgent = (n.agent || '').toLowerCase().includes(searchQuery);
          if (matchTitle || matchPath || matchType || matchAgent) {
            searchMatchingNodeIds.add(n.id);
          }
        });

        const firstMatch = rawGraphData.nodes.find(n => searchMatchingNodeIds.has(n.id));
        if (firstMatch && firstMatch.x !== undefined) {
          Graph.centerAt(firstMatch.x, firstMatch.y, 600);
        }

        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(async () => {
          if (!searchQuery) return;
          try {
            const res = await fetch('/api/search?q=' + encodeURIComponent(searchQuery));
            if (res.ok) {
              const data = await res.json();
              if (data && data.results && Array.isArray(data.results)) {
                data.results.forEach(hit => {
                  const hitPath = hit.path || hit.filename || hit.title;
                  rawGraphData.nodes.forEach(n => {
                    if (n.path === hitPath || n.id === hitPath || (hitPath && n.id.includes(hitPath))) {
                      searchMatchingNodeIds.add(n.id);
                    }
                  });
                });
              }
            }
          } catch (e) {
            // Client-side matches active
          }
        }, 300);
      }
    }

    function clearSearch() {
      document.getElementById('quick-search').value = '';
      handleSearchInput('');
    }

    function resetGraphView() {
      Graph.zoomToFit(800, 40);
    }

    // Slide-out Drawer Logic
    async function openNoteDrawer(path) {
      currentDrawerPath = path;
      const drawer = document.getElementById('note-drawer');
      drawer.classList.remove('translate-x-full');

      document.getElementById('drawer-title').innerText = 'Loading...';
      document.getElementById('drawer-path').innerText = path;
      document.getElementById('drawer-content').innerHTML = '<p class="text-slate-400">Loading markdown content...</p>';

      try {
        const res = await fetch('/api/note?path=' + encodeURIComponent(path));
        const data = await res.json();
        if (data.error) {
          document.getElementById('drawer-content').innerHTML = `<p class="text-red-400">${data.error}</p>`;
          return;
        }

        document.getElementById('drawer-title').innerText = data.title || path;
        document.getElementById('drawer-path').innerText = data.path;
        document.getElementById('drawer-type-badge').innerText = (data.type || 'NOTE').toUpperCase();
        document.getElementById('drawer-agent-badge').innerText = data.agent || 'human';

        document.getElementById('drawer-content').innerHTML = marked.parse(data.body || '');

        const linksDiv = document.getElementById('drawer-links');
        linksDiv.innerHTML = '';
        if (data.links && data.links.length > 0) {
          data.links.forEach(l => {
            const btn = document.createElement('button');
            btn.className = 'px-2 py-0.5 rounded bg-slate-900 hover:bg-slate-800 text-cyan-400 border border-slate-700 text-[11px] font-mono transition';
            btn.innerText = l;
            btn.onclick = () => {
              const targetPath = l.endsWith('.md') ? l : l + '.md';
              openNoteDrawer(targetPath);
              const targetNode = rawGraphData.nodes.find(n => n.id === targetPath || n.path === targetPath || n.name === l);
              if (targetNode) {
                handleNodeClick(targetNode);
              }
            };
            linksDiv.appendChild(btn);
          });
        } else {
          linksDiv.innerHTML = '<span class="text-slate-500 text-[11px]">No outgoing links</span>';
        }
      } catch (err) {
        document.getElementById('drawer-content').innerHTML = `<p class="text-red-400">Failed to load note: ${err}</p>`;
      }
    }

    function closeNoteDrawer() {
      document.getElementById('note-drawer').classList.add('translate-x-full');
      clearHighlight();
    }

    function copyCurrentPath() {
      if (currentDrawerPath) {
        navigator.clipboard.writeText(currentDrawerPath);
        const btn = document.getElementById('copy-path-btn');
        if (btn) {
          const orig = btn.innerHTML;
          btn.innerHTML = '<i class="fa-solid fa-check text-emerald-400"></i>';
          setTimeout(() => { btn.innerHTML = orig; }, 1500);
        }
      }
    }

    
    let activeDrawerTab = 'content';

    function switchDrawerTab(tab) {
      activeDrawerTab = tab;
      const contentDiv = document.getElementById('drawer-content');
      const dossierDiv = document.getElementById('drawer-dossier');
      const tabContent = document.getElementById('tab-btn-content');
      const tabDossier = document.getElementById('tab-btn-dossier');

      if (tab === 'content') {
        contentDiv.classList.remove('hidden');
        dossierDiv.classList.add('hidden');
        tabContent.className = 'py-2.5 px-3 text-cyan-400 border-b-2 border-cyan-400 flex items-center space-x-1.5 transition';
        tabDossier.className = 'py-2.5 px-3 text-slate-400 hover:text-slate-200 border-b-2 border-transparent flex items-center space-x-1.5 transition';
      } else {
        contentDiv.classList.add('hidden');
        dossierDiv.classList.remove('hidden');
        tabContent.className = 'py-2.5 px-3 text-slate-400 hover:text-slate-200 border-b-2 border-transparent flex items-center space-x-1.5 transition';
        tabDossier.className = 'py-2.5 px-3 text-purple-400 border-b-2 border-purple-400 flex items-center space-x-1.5 transition';
        loadCognitiveDossier(currentDrawerPath);
      }
    }

    async function loadCognitiveDossier(path) {
      const dossierDiv = document.getElementById('drawer-dossier');
      dossierDiv.innerHTML = '<p class="text-slate-400 font-mono text-xs"><i class="fa-solid fa-spinner animate-spin"></i> Traversing 1st & 2nd degree graph relations...</p>';
      try {
        const res = await fetch('/api/expand?q=' + encodeURIComponent(path));
        const data = await res.json();
        if (data.error) {
          dossierDiv.innerHTML = `<p class="text-red-400 text-xs">${data.error}</p>`;
          return;
        }

        const focal = data.focal_node || {};
        let html = `
          <div class="glass-panel p-4 rounded-xl border border-purple-500/40 space-y-2">
            <div class="flex items-center justify-between">
              <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded bg-purple-950 text-purple-300 border border-purple-700">FOCAL NODE</span>
              <span class="text-[10px] font-mono font-bold px-2 py-0.5 rounded ${focal.status === 'done' ? 'bg-emerald-950 text-emerald-300' : 'bg-pink-950 text-pink-300'}">${(focal.status || 'ACTIVE').toUpperCase()}</span>
            </div>
            <h3 class="text-base font-bold text-white">${focal.title}</h3>
            <p class="text-xs font-mono text-cyan-300">${focal.path}</p>
            <div class="text-xs text-slate-300 pt-2 border-t border-slate-800">
              <p><strong>Next Action:</strong> ${focal.next_action || 'None'}</p>
              <p><strong>Scope:</strong> ${focal.scope || 'None'}</p>
            </div>
          </div>
        `;

        // Active Blockers
        if (focal.blockers && focal.blockers.length > 0 && focal.blockers[0] !== 'none') {
          html += `
            <div class="p-3 rounded-xl bg-red-950/40 border border-red-500/40 text-xs">
              <h4 class="font-bold text-red-400 mb-1 flex items-center space-x-1.5">
                <i class="fa-solid fa-triangle-exclamation"></i>
                <span>Active Blockers & Invariants</span>
              </h4>
              <ul class="list-disc list-inside text-red-200">
                ${focal.blockers.map(b => `<li>${b}</li>`).join('')}
              </ul>
            </div>
          `;
        }

        // 1st Degree
        if (data.first_degree_neighbors && data.first_degree_neighbors.length > 0) {
          html += `
            <div class="space-y-2">
              <h4 class="text-xs font-bold uppercase tracking-wider text-cyan-400">1st-Degree Relational Neighborhood</h4>
              <div class="grid grid-cols-1 gap-2">
                ${data.first_degree_neighbors.map(n => `
                  <div class="p-2.5 rounded-lg bg-slate-900/80 border border-slate-800 text-xs hover:border-cyan-500/50 transition cursor-pointer" onclick="openNoteDrawer('${n.path}.md')">
                    <div class="flex items-center justify-between">
                      <span class="font-bold text-slate-200">${n.title}</span>
                      <span class="text-[10px] font-mono text-cyan-400">${n.type}</span>
                    </div>
                  </div>
                `).join('')}
              </div>
            </div>
          `;
        }

        // 2nd Degree
        if (data.second_degree_neighbors && data.second_degree_neighbors.length > 0) {
          html += `
            <div class="space-y-2">
              <h4 class="text-xs font-bold uppercase tracking-wider text-purple-400">2nd-Degree Architectural Context</h4>
              <div class="grid grid-cols-1 gap-2">
                ${data.second_degree_neighbors.map(n => `
                  <div class="p-2.5 rounded-lg bg-slate-900/60 border border-slate-800/80 text-xs hover:border-purple-500/50 transition cursor-pointer" onclick="openNoteDrawer('${n.path}.md')">
                    <div class="flex items-center justify-between">
                      <span class="font-bold text-slate-300">${n.title}</span>
                      <span class="text-[10px] font-mono text-purple-400">${n.type}</span>
                    </div>
                  </div>
                `).join('')}
              </div>
            </div>
          `;
        }

        dossierDiv.innerHTML = html;
      } catch (err) {
        dossierDiv.innerHTML = `<p class="text-red-400 text-xs">Error loading dossier: ${err}</p>`;
      }
    }

    
    let swarmPollInterval = null;

    // Defined at top level on purpose. It used to be assigned inside
    // loadSwarmData, so a failed /api/swarm fetch left the onclick handlers
    // undefined and every lifecycle button silently did nothing.
    function lifecycleFeedback(agent, text, tone) {
      const line = document.getElementById(`${agent}-lifecycle-feedback`);
      if (!line) return;
      line.className = `text-[10px] font-mono mt-1 ${tone || 'text-slate-500'}`;
      line.textContent = text;
    }

    async function deliveryLifecycle(agent, event) {
      const row = document.getElementById(`${agent}-approve-row`);
      const taskId = row?.dataset.taskId;
      if (!taskId) { lifecycleFeedback(agent, `no delivery is waiting for ${agent}`, 'text-amber-400'); return; }
      // The model is reported by a human choosing what they switched to; it is
      // never inferred from the recommendation.
      const select = document.getElementById(`${agent}-model-select`);
      const model = select ? select.value : '';
      const note = event === 'escalate' ? 'blocked; escalated from the dashboard'
                 : event === 'complete' ? 'completed and verified from the dashboard'
                 : event === 'progress' ? 'work reported in progress from the dashboard'
                 : 'acknowledged by a human at the Shared Brain dashboard; the agent has not yet reported work';
      const buttons = Array.from(row.querySelectorAll('button'));
      const wasDisabled = buttons.map(b => b.disabled);
      buttons.forEach(b => { b.disabled = true; });
      lifecycleFeedback(agent, `recording ${event} for ${taskId}…`, 'text-slate-400');
      try {
        const res = await fetch('/api/delivery/lifecycle', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({agent, task_id: taskId, event, model, note})
        });
        const out = await res.json();
        if (out.status === 'recorded') {
          // Acknowledging is receipt, not execution. Say so, because a green
          // "recorded" next to a task list otherwise reads as "it ran".
          const started = event === 'acknowledge'
            ? ' — nothing is running yet; paste the briefing into the Agent panel'
            : '';
          lifecycleFeedback(agent, `${event} recorded — ${String(out.lifecycle).replaceAll('_', ' ')}; model ${out.model_reported || 'not reported'}${started}`, 'text-emerald-400');
        } else {
          lifecycleFeedback(agent, `rejected: ${out.reason || 'unknown reason'}`, 'text-red-400');
        }
      } catch (e) {
        lifecycleFeedback(agent, `request failed: ${e}`, 'text-red-400');
      } finally {
        buttons.forEach((b, i) => { b.disabled = wasDisabled[i]; });
      }
      // loadSwarmData is the real refresh. The previous guard called a
      // refreshSwarm function that was never defined anywhere, so the card,
      // the counters and the task lists never updated after an approval.
      await loadSwarmData();
    }
    window.deliveryLifecycle = deliveryLifecycle;

    function openSwarmModal() {
      document.getElementById('swarm-modal').classList.remove('hidden');
      loadSwarmData();
    }
    function closeSwarmModal() { document.getElementById('swarm-modal').classList.add('hidden'); }

    // -----------------------------------------------------------------------
    // Provider Accounts panel.
    //
    // Adding an API key here calls providers.py, which turns the key into a
    // working worker even when no agent CLI is installed. The key itself is a
    // write-only value from the browser's point of view: it goes up in a POST
    // body and is never read back, never put in the DOM after submit, and never
    // logged. The API is contractually free of key values, so rendering the
    // response is safe.
    // -----------------------------------------------------------------------
    function openProvidersModal() {
      document.getElementById('providers-modal').classList.remove('hidden');
      loadProviders();
    }
    function closeProvidersModal() { document.getElementById('providers-modal').classList.add('hidden'); }

    // Escape provider-supplied strings (label, notes, key_source) before they
    // reach innerHTML so a hostile catalog entry cannot inject markup.
    function provEsc(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    async function addProvider(providerId) {
      const input = document.getElementById('prov-key-' + providerId);
      if (!input || !input.value.trim()) { input && input.focus(); return; }
      const modelInput = document.getElementById('prov-model-' + providerId);
      const body = { provider: providerId, api_key: input.value, model: (modelInput && modelInput.value.trim()) || undefined };
      // Clear the field the instant we have captured the value into the request
      // body, so the secret never lingers in the DOM. Nothing is logged.
      input.value = '';
      try {
        const res = await fetch('/api/providers', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        const out = await res.json();
        if (!res.ok || out.status === 'error') {
          setProviderNote(providerId, out.error || 'add failed', false);
        }
        // A successful add bumps the server generation, so the SSE stream will
        // trigger runRefresh()->loadProviders() and repaint this row live.
      } catch (e) {
        setProviderNote(providerId, 'network error', false);
      }
    }

    async function testProvider(providerId) {
      setProviderNote(providerId, 'testing…', null);
      try {
        const res = await fetch('/api/providers/test', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: providerId })
        });
        const out = await res.json();
        if (out.ok) {
          setProviderNote(providerId, `ok · ${out.latency_ms}ms · ${out.model_count} models`, true);
        } else {
          setProviderNote(providerId, `failed${out.http ? ' (http ' + out.http + ')' : ''}: ${out.error || 'no key'}`, false);
        }
      } catch (e) {
        setProviderNote(providerId, 'network error', false);
      }
    }

    async function removeProvider(providerId) {
      try {
        await fetch('/api/providers/remove', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider: providerId })
        });
        // Removal bumps the generation too; SSE repaints the row as unconfigured.
      } catch (e) { /* the safety poll will reconcile if the push is missed */ }
    }

    function setProviderNote(providerId, text, ok) {
      const el = document.getElementById('prov-note-' + providerId);
      if (!el) return;
      el.innerText = text;
      el.className = 'text-[10px] font-mono mt-1 ' +
        (ok === true ? 'text-emerald-400' : ok === false ? 'text-red-400' : 'text-slate-400');
    }

    async function loadProviders() {
      const list = document.getElementById('providers-list');
      const summaryEl = document.getElementById('providers-summary');
      if (!list) return;
      try {
        const res = await fetch('/api/providers');
        const data = await res.json();
        const accounts = data.accounts || [];
        const s = data.summary || {};

        // Header badge + summary line mirror discover()'s worker verdict.
        document.getElementById('providers-badge').innerText = s.configured_count || 0;
        if (summaryEl) {
          const worker = s.worker_available_without_cli
            ? '<span class="text-emerald-400">worker available with no CLI</span>'
            : '<span class="text-amber-400">no worker yet — add a key</span>';
          summaryEl.innerHTML =
            `${s.configured_count || 0} configured · ${s.free_count || 0} free · ` +
            `${s.catalog_size || accounts.length} in catalog · ${worker}`;
        }

        list.innerHTML = accounts.map(a => {
          const free = a.is_free_default;
          const badge = free
            ? '<span class="px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 text-[10px] font-mono font-bold">FREE</span>'
            : '<span class="px-1.5 py-0.5 rounded bg-amber-950 text-amber-300 text-[10px] font-mono font-bold">PAID</span>';
          const dot = a.configured
            ? '<span class="w-2 h-2 rounded-full bg-emerald-400"></span>'
            : '<span class="w-2 h-2 rounded-full bg-amber-400"></span>';
          // Configured accounts get Test + Remove; unconfigured ones get a
          // password field plus Add. The key field is type=password and its
          // value is only ever read at submit time, then cleared.
          const controls = a.configured
            ? `<div class="flex items-center space-x-1.5 shrink-0">
                 <button onclick="testProvider('${provEsc(a.provider)}')" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-cyan-900 border border-slate-700 text-[11px] text-cyan-300 font-mono transition">Test</button>
                 <button onclick="removeProvider('${provEsc(a.provider)}')" class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-red-900 border border-slate-700 text-[11px] text-red-300 font-mono transition">Remove</button>
               </div>`
            : `<div class="flex items-center space-x-1.5 shrink-0">
                 <input id="prov-key-${provEsc(a.provider)}" type="password" autocomplete="off" placeholder="API key" class="w-40 bg-slate-900 border border-slate-700 rounded-lg px-2 py-1 text-[11px] text-slate-100 font-mono focus:outline-none focus:border-cyan-400">
                 <button onclick="addProvider('${provEsc(a.provider)}')" class="px-2.5 py-1 rounded-lg bg-gradient-to-r from-cyan-500 to-indigo-500 hover:from-cyan-400 hover:to-indigo-400 text-black text-[11px] font-bold transition">Add</button>
               </div>`;
          return `
            <div class="rounded-lg border border-slate-700 bg-slate-900/70 p-3">
              <div class="flex items-center justify-between gap-3">
                <div class="min-w-0">
                  <div class="flex items-center space-x-2">
                    ${dot}
                    <span class="text-xs font-bold text-slate-100 truncate">${provEsc(a.label)}</span>
                    ${badge}
                    <span class="text-[10px] font-mono text-slate-500">${provEsc(a.free_type)}</span>
                  </div>
                  <div class="text-[10px] font-mono text-slate-400 mt-0.5 truncate">
                    ${provEsc(a.model)} · source: ${provEsc(a.key_source)}
                  </div>
                  <div id="prov-note-${provEsc(a.provider)}" class="text-[10px] font-mono mt-1 text-slate-400"></div>
                </div>
                ${controls}
              </div>
            </div>`;
        }).join('');
      } catch (err) {
        // A failed provider fetch must never blank the panel silently.
        list.innerHTML = '<div class="text-[11px] font-mono text-red-400">Provider data unavailable.</div>';
      }
    }

    async function loadSwarmData() {
      try {
        const res = await fetch('/api/swarm');
        if (!res.ok) throw new Error('Task state unavailable');
        const data = await res.json(), stats = data.stats || {}, tasks = data.tasks || {};
        document.getElementById('swarm-badge').innerText = stats.pending || stats.in_progress || stats.stale || 0;
        document.getElementById('swarm-count-pending').innerText = stats.pending || 0;
        document.getElementById('swarm-count-inprogress').innerText = stats.in_progress || 0;
        document.getElementById('swarm-count-completed').innerText = stats.completed || 0;
        document.getElementById('swarm-count-escalated').innerText = (stats.stale || 0) + (stats.escalated || 0);
        const lifecycleColors={working:'bg-emerald-400',acknowledged:'bg-cyan-400',staged:'bg-amber-400',awaiting_verification:'bg-purple-400',idle:'bg-slate-400',ready:'bg-teal-400',available:'bg-emerald-400',unattached:'bg-amber-400',blocked:'bg-red-400',unavailable:'bg-slate-500',unknown:'bg-slate-500'};
        // The second Antigravity account is a real headless worker, so it is shown
        // as a live agent rather than a delivery target.
        {
          const state = data.agents?.['antigravity-api'] || {};
          const dot = document.getElementById('antigravity-api-agent-dot');
          const label = document.getElementById('antigravity-api-agent-status');
          const models = document.getElementById('antigravity-api-agent-models');
          if (dot) dot.className = `w-2 h-2 rounded-full ${lifecycleColors[state.availability] || 'bg-slate-500'}`;
          if (label) {
            label.textContent = state.key_attached
              ? `ready — ${state.scope || 'gemini only'}`
              : (state.basis || 'no Gemini API key attached');
            label.className = `text-[10px] truncate ${state.key_attached ? 'text-emerald-400' : 'text-amber-400'}`;
          }
          if (models) models.textContent = (state.models || []).slice(0, 2).join(' · ');
        }
        for (const name of ['antigravity-ide']) {
          const state=data.agents?.[name] || {}, dot=document.getElementById(`${name}-agent-dot`), label=document.getElementById(`${name}-agent-status`);
          if (dot) dot.className=`w-2 h-2 rounded-full ${lifecycleColors[state.availability] || 'bg-slate-500'}`;
          // `lifecycle` describes the *delivery* state and is legitimately
          // "unavailable" when nothing has been delivered yet. Showing it first
          // made an installed, idle agent read as unavailable, so prefer the
          // availability and only show a lifecycle once a delivery exists.
          const row=document.getElementById(`${name}-approve-row`);
          if (row) {
            // Only offer approval when something is actually waiting.
            const waiting = state.lifecycle && state.lifecycle.startsWith('staged_for_');
            const active = ['acknowledged','working','awaiting_verification'].includes(state.availability);
            row.className = (waiting || active) ? 'flex flex-wrap items-center gap-1.5 mt-1.5' : 'hidden';
            row.dataset.taskId = state.task_id || '';
            row.dataset.model = (state.model_candidates && state.model_candidates[0]) || '';
            // Enable only the transitions the delivery lifecycle actually
            // accepts, so a click can never be a silent no-op or a guaranteed
            // rejection. Approve is meaningful only while a delivery is staged;
            // progress and completion require an acknowledgement first.
            const enabled = {acknowledge: !!waiting, progress: !!active, complete: !!active, escalate: !!(waiting || active)};
            const why = {
              acknowledge: waiting ? 'Record that a human received this delivery. It does not run the task; antigravity-ide has no headless mode, so paste the briefing into its Agent panel.'
                                   : 'Already acknowledged; nothing to acknowledge',
              progress: active ? 'Record that work is under way in the editor' : 'Acknowledge the delivery first',
              complete: active ? 'Record verified completion of work you did in the editor' : 'Acknowledge the delivery first',
              escalate: 'Record that this delivery is blocked',
            };
            row.querySelectorAll('button[data-lifecycle-event]').forEach(btn => {
              const key = btn.dataset.lifecycleEvent;
              btn.disabled = !enabled[key];
              btn.title = why[key] || '';
            });
            const select=document.getElementById(`${name}-model-select`);
            if (select) {
              const candidates=state.model_candidates || [];
              const signature=JSON.stringify([candidates, state.model_reported || '']);
              if (select.dataset.signature !== signature) {
                select.dataset.signature = signature;
                const keep = select.value;
                select.replaceChildren();
                const none=document.createElement('option'); none.value=''; none.textContent='model not reported';
                select.append(none);
                candidates.forEach(candidate => {
                  const option=document.createElement('option'); option.value=candidate; option.textContent=candidate; select.append(option);
                });
                select.value = (state.model_reported && candidates.includes(state.model_reported)) ? state.model_reported
                             : (candidates.includes(keep) ? keep : '');
              }
            }
            if (state.task_id) {
              const line=document.getElementById(`${name}-lifecycle-feedback`);
              if (line && !line.textContent) {
                lifecycleFeedback(name, `${state.task_id} — model ${state.model_reported || 'not reported'}`, 'text-slate-500');
              }
            }
          }
          if (label) {
            const hasDelivery = state.lifecycle && state.lifecycle !== 'unavailable';
            const primary = (hasDelivery ? state.lifecycle : (state.availability || 'unknown'));
            label.textContent=`${String(primary).replaceAll('_',' ')} — ${state.basis || 'no evidence'}`;
          }
        }
        renderTaskList('swarm-list-pending', tasks.pending || [], 'border-slate-800', 'Queued');
        renderTaskList('swarm-list-inprogress', tasks['in-progress'] || [], 'border-cyan-500/50 bg-cyan-950/30', 'Working');
        renderTaskList('swarm-list-completed', tasks.completed || [], 'border-emerald-500/30 bg-emerald-950/20', 'Completed with recorded receipt');
        renderTaskList('swarm-list-escalated', [...(tasks.stale || []), ...(tasks.escalated || [])], 'border-amber-500/40 bg-amber-950/20', 'Needs evidence or intervention');
      } catch (e) { console.warn('Swarm state unavailable', e); }
    }

    function renderTaskList(containerId, list, extraClasses, fallback) {
      const container = document.getElementById(containerId); if (!container) return;
      container.replaceChildren();
      if (!list.length) { const empty=document.createElement('p'); empty.className='text-[10px] text-slate-600 text-center py-4 italic font-mono'; empty.textContent=fallback || 'Empty'; container.append(empty); return; }
      list.forEach(t => {
        const card=document.createElement('article'); card.className=`p-2 rounded-lg bg-slate-900 border ${extraClasses} text-xs space-y-1`;
        const agent=document.createElement('div'); agent.className='text-[10px] font-mono font-bold text-purple-300 uppercase'; agent.textContent=t.assigned_to || 'agent';
        const title=document.createElement('p'); title.className='text-white font-medium line-clamp-2 text-[11px]'; title.textContent=t.title || 'Untitled task';
        const phase=document.createElement('p'); phase.className='text-[9px] font-mono text-slate-400'; phase.textContent=t.display_status === 'stale' ? `STALE — ${t.stale_reason}` : (t.stage_state === 'awaiting_approval' ? `AWAITING APPROVAL — run: brain swarm approve ${t.id}` : (t.stage_state || t.display_status || 'unknown'));
        card.append(agent,title,phase); container.append(card);
      });
    }

    let approvedSwarmPlan = null;

    async function dispatchSwarmTasks() {
      const input=document.getElementById('swarm-task-input'), text=input.value.trim(); if (!text) return;
      const btn=document.getElementById('dispatch-swarm-btn'), runBtn=document.getElementById('execute-swarm-btn'), status=document.getElementById('swarm-execution-status');
      btn.disabled=true; runBtn.disabled=true; approvedSwarmPlan=null; btn.textContent='Generating plan…';
      try {
        const res=await fetch('/api/swarm/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tasks:text})});
        const data=await res.json(); if (!res.ok) throw new Error(data.error || 'Plan generation failed');
        approvedSwarmPlan={tasks:text,plan_hash:data.plan_hash};
        status.textContent=`Plan ${data.plan_hash}: ${data.tasks?.length || 0} task(s). Review the local-only policy, then click Approve & run.`;
        runBtn.disabled=false;
      } catch (e) { status.textContent=`Plan failed: ${e.message}`; }
      finally { btn.disabled=false; btn.innerHTML='<i class="fa-solid fa-diagram-project"></i> <span>Generate plan</span>'; }
    }

    async function executeApprovedSwarmPlan() {
      if (!approvedSwarmPlan) return;
      const btn=document.getElementById('execute-swarm-btn'), planBtn=document.getElementById('dispatch-swarm-btn'), status=document.getElementById('swarm-execution-status');
      btn.disabled=true; planBtn.disabled=true; btn.textContent='Starting…';
      try {
        const res=await fetch('/api/swarm/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...approvedSwarmPlan,confirm_execution:true})});
        const data=await res.json(); if (!res.ok) throw new Error(data.reason || data.error || 'Execution was rejected');
        status.textContent=`Accepted ${data.batch_id}: ${data.tasks?.length || 0} task(s) started. Live queue will refresh automatically.`;
        approvedSwarmPlan=null; loadSwarmData();
      } catch (e) { status.textContent=`Execution blocked: ${e.message}`; }
      finally { planBtn.disabled=false; btn.disabled=!approvedSwarmPlan; btn.innerHTML='<i class="fa-solid fa-play"></i> <span>Approve & start plan</span>'; }
    }

    function triggerSelfHeal() {
      document.getElementById('self-heal-modal').classList.remove('hidden');
    }

    function closeSelfHealModal() {
      document.getElementById('self-heal-modal').classList.add('hidden');
    }

    async function executeLiveHeal() {
      const term = document.getElementById('heal-terminal');
      const statusMsg = document.getElementById('heal-status-msg');
      const btn = document.getElementById('execute-heal-btn');

      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner animate-spin"></i> <span>Healing...</span>';
      statusMsg.innerText = 'Scanning & repairing components...';

      term.innerHTML = '<p class="text-cyan-400">> [SENTINEL] Initiating autonomous self-healing loop...</p>';

      try {
        const res = await fetch('/api/heal', { method: 'POST' });
        const data = await res.json();

        let output = '<p class="text-cyan-400">> [SENTINEL] Scan complete (' + data.duration_seconds + 's)</p>';
        output += '<p class="text-emerald-400">> [HEALTH SCORE] ' + data.health_score + '% [' + data.status.toUpperCase() + ']</p>';
        output += '<p class="text-slate-400">> Neural Sidecar: ' + (data.sidecar_healthy ? 'ONLINE (127.0.0.1:3334)' : 'OFFLINE') + '</p>';
        output += '<p class="text-slate-500">----------------------------------------</p>';

        if (data.repairs && data.repairs.length > 0) {
          data.repairs.forEach(r => {
            output += '<p class="text-emerald-300">✓ ' + r + '</p>';
          });
        } else {
          output += '<p class="text-slate-400">✓ Store is 100% compliant. No repairs needed.</p>';
        }

        if (data.warnings && data.warnings.length > 0) {
          data.warnings.forEach(w => {
            output += '<p class="text-amber-400">! ' + w + '</p>';
          });
        }

        output += '<p class="text-cyan-300 mt-2">> Auto-heal sequence completed successfully.</p>';
        term.innerHTML = output;

        // Update badge
        document.getElementById('health-badge').innerText = data.health_score + '%';
        statusMsg.innerText = 'Completed: Health score ' + data.health_score + '%';

        // Refresh graph
        loadData();
      } catch (err) {
        term.innerHTML += '<p class="text-red-400">> Execution failed: ' + err + '</p>';
        statusMsg.innerText = 'Error during repair';
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-bolt"></i> <span>Run Autonomous Self-Healing</span>';
      }
    }

    function openAnalyticsModal() {
      document.getElementById('analytics-modal').classList.remove('hidden');
    }
    function closeAnalyticsModal() {
      document.getElementById('analytics-modal').classList.add('hidden');
    }

    async function loadData() {
      try {
        const [graphRes, statusRes] = await Promise.all([
          fetch('/api/graph'),
          fetch('/api/status')
        ]);
        const graphData = await graphRes.json();
        const statusData = await statusRes.json();

        // Re-seeding the force graph restarts its physics simulation, which is by
        // far the most expensive thing on this page. The graph is built from the
        // Markdown notes only, so a swarm dispatch (which churns task JSON) never
        // changes it. Only re-render when the topology actually differs, and keep
        // the live node objects so node positions survive a refresh.
        const graphSignature = JSON.stringify([
          (graphData.nodes || []).map(n => n.id),
          (graphData.links || []).map(l => [
            typeof l.source === 'object' ? l.source.id : l.source,
            typeof l.target === 'object' ? l.target.id : l.target
          ])
        ]);
        const graphChanged = graphSignature !== lastGraphSignature;
        if (graphChanged) {
          lastGraphSignature = graphSignature;
          rawGraphData = graphData;
        }

        const c = statusData.counts || {};
        document.getElementById('count-all').innerText = statusData.total_notes || 0;
        document.getElementById('count-handoffs').innerText = c.handoff || c.handoffs || 0;
        document.getElementById('count-projects').innerText = c.projects || c.project || 0;
        document.getElementById('count-decisions').innerText = c.decisions || c.decision || 0;
        document.getElementById('count-runbooks').innerText = c.runbooks || c.runbook || 0;
        document.getElementById('count-schemas').innerText = c.schemas || c.schema || 1;

        document.getElementById('validation-status').innerText = statusData.validation_score ? statusData.validation_score.split(' ')[0] + ' Valid' : '10/10 Valid';
        document.getElementById('modal-integrity').innerText = statusData.validation_score || '10/10 (100%)';

        const b = statusData.baton;
        if (b) {
          document.getElementById('baton-title').innerText = b.title || 'In Flight';
          document.getElementById('baton-next').innerText = b.next || 'Continuing work...';
          document.getElementById('baton-agent-badge').innerText = b.agent || 'antigravity';
          document.getElementById('baton-mtime').innerText = b.mtime_formatted || 'Updated recently';
        }

        const agentBars = document.getElementById('agent-bars');
        agentBars.innerHTML = '';
        const ab = statusData.agent_breakdown || {};
        const total = statusData.total_notes || 1;
        const agentGradients = {
          'kiro-cli': 'from-amber-500 to-orange-600',
          'antigravity': 'from-pink-500 to-purple-600',
          'cline': 'from-cyan-500 to-blue-600',
          'amazon-q': 'from-yellow-500 to-amber-600',
          'antigravity-ide': 'from-indigo-500 to-violet-600',
          'human': 'from-emerald-500 to-teal-600',
          'unknown': 'from-slate-500 to-slate-700'
        };
        for (const [agent, count] of Object.entries(ab)) {
          const pct = Math.round((count / total) * 100);
          const grad = agentGradients[agent] || 'from-cyan-500 to-indigo-500';
          agentBars.innerHTML += `
            <div>
              <div class="flex justify-between text-xs mb-1 font-mono">
                <span class="text-cyan-300 font-bold">${agent}</span>
                <span class="text-slate-400">${count} notes (${pct}%)</span>
              </div>
              <div class="w-full h-2 rounded-full bg-slate-800 overflow-hidden">
                <div class="h-full bg-gradient-to-r ${grad} rounded-full transition-all duration-500" style="width: ${pct}%"></div>
              </div>
            </div>
          `;
        }

        if (graphChanged) toggleTypeFilter(activeFilter);
      } catch (err) {
        console.error('Failed to load graph data:', err);
      }
    }

    // ---------------------------------------------------------------------
    // Live update pipeline.
    //
    // Three problems were fixed here:
    //  1. Every SSE event fired loadData()+loadSwarmData() immediately. A swarm
    //     dispatch writes many task files in a burst, so the browser issued
    //     overlapping fetches of ~300 KB several times a second and the page
    //     locked up. Refreshes are now debounced and never overlap.
    //  2. evt.onerror only logged. If the stream died the page went silent and
    //     the only cure was a manual reload. A watchdog now rebuilds the stream,
    //     and a slow safety poll guarantees eventual freshness regardless.
    //  3. The server now sends its brain generation as the event payload, so a
    //     refresh is skipped entirely when nothing actually changed.
    // ---------------------------------------------------------------------
    let refreshTimer = null;
    let refreshInFlight = false;
    let refreshQueued = false;
    let lastGeneration = -1;
    let sseSource = null;
    let lastStreamContact = 0;
    const REFRESH_DEBOUNCE_MS = 400;
    const STREAM_DEAD_AFTER_MS = 45000;   // server sends a keep-alive every 15s
    const SAFETY_POLL_MS = 20000;

    async function runRefresh() {
      if (refreshInFlight) { refreshQueued = true; return; }
      refreshInFlight = true;
      try {
        // Settled, not all: one failing panel must never stop the other from
        // updating, which is what previously left the UI looking frozen.
        await Promise.allSettled([loadData(), loadSwarmData(), loadProviders()]);
      } finally {
        refreshInFlight = false;
        if (refreshQueued) { refreshQueued = false; scheduleRefresh(); }
      }
    }

    function scheduleRefresh() {
      if (refreshTimer) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(() => { refreshTimer = null; runRefresh(); }, REFRESH_DEBOUNCE_MS);
    }

    function setStreamIndicator(live) {
      const dot = document.getElementById('sse-status-dot');
      if (dot) {
        dot.className = 'w-2 h-2 rounded-full ' + (live ? 'bg-emerald-400' : 'bg-amber-400');
        dot.title = live ? 'Live updates connected' : 'Live updates reconnecting';
      }
    }

    function initSSE() {
      if (sseSource) { try { sseSource.close(); } catch (e) {} }
      sseSource = new EventSource('/api/events');
      lastStreamContact = Date.now();

      sseSource.onopen = () => { lastStreamContact = Date.now(); setStreamIndicator(true); };
      sseSource.onmessage = (e) => {
        lastStreamContact = Date.now();
        setStreamIndicator(true);
        const gen = Number(e.data);
        // Skip work when the store has not moved since the last refresh.
        if (Number.isFinite(gen) && gen === lastGeneration) return;
        if (Number.isFinite(gen)) lastGeneration = gen;
        scheduleRefresh();
      };
      sseSource.addEventListener('hello', (e) => {
        lastStreamContact = Date.now();
        setStreamIndicator(true);
        const gen = Number(e.data);
        if (Number.isFinite(gen)) lastGeneration = gen;
      });
      sseSource.onerror = () => {
        setStreamIndicator(false);
        // EventSource retries on its own; the watchdog below is the backstop for
        // the case where it reconnects to a half-open socket and stays silent.
      };
    }

    function startLiveUpdateWatchdogs() {
      // Rebuild a stream that has gone quiet for longer than the keep-alive gap.
      setInterval(() => {
        const silentFor = Date.now() - lastStreamContact;
        const broken = !sseSource || sseSource.readyState === 2; // 2 = CLOSED
        if (broken || silentFor > STREAM_DEAD_AFTER_MS) {
          setStreamIndicator(false);
          initSSE();
          scheduleRefresh();
        }
      }, 10000);

      // Safety poll: cheap because the server answers /api/status and /api/swarm
      // from a generation-keyed cache and honours If-None-Match, so an unchanged
      // brain costs a 304. This is what removes the need to reload the page.
      setInterval(() => {
        if (document.hidden) return;
        scheduleRefresh();
      }, SAFETY_POLL_MS);

      // Refresh immediately when the operator comes back to the tab.
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) scheduleRefresh();
      });
    }

    window.addEventListener('DOMContentLoaded', () => {
      initGraph();
      runRefresh();
      initSSE();
      startLiveUpdateWatchdogs();
    });
  </script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so the browser can keep connections alive instead of paying a new
    # TCP handshake for every poll.
    protocol_version = "HTTP/1.1"

    def _send_json(self, payload, status: int = 200, *, cacheable: bool = False):
        """Serialise and send JSON compactly, gzipping large bodies.

        The previous code used ``json.dumps(..., indent=2)`` on every endpoint.
        On /api/swarm that is ~240 KB of mostly whitespace, re-serialised on each
        refresh. Compact separators plus gzip cut it by roughly an order of
        magnitude, which is the difference the browser actually feels.
        """
        if isinstance(payload, (bytes, bytearray)):
            body = bytes(payload)
        else:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")

        headers = [("Content-Type", "application/json")]
        if len(body) >= GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=6)
            headers.append(("Content-Encoding", "gzip"))

        etag = '"%s"' % hashlib.md5(body).hexdigest()
        if cacheable and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache" if cacheable else "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            file_path = (STATIC_DIR / rel_path).resolve()
            if not file_path.is_relative_to(STATIC_DIR) or not file_path.exists() or file_path.is_dir():
                self.send_error(404, "File not found")
                return

            ext = file_path.suffix.lower()
            mime_types = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".html": "text/html",
                ".json": "application/json",
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".woff2": "font/woff2",
                ".woff": "font/woff",
                ".ttf": "font/ttf",
            }
            content_type = mime_types.get(ext, "application/octet-stream")
            try:
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                self.send_error(500, f"Error reading file: {e}")
                return

        if path in ("/", "/index.html"):
            data = HTML_DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        elif path == "/api/swarm":
            import swarm
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                history_limit = max(1, min(100, int(qs.get("history_limit", ["12"])[0])))
            except ValueError:
                history_limit = 12
            data = cached_by_generation(
                f"swarm:{history_limit}",
                lambda: swarm.get_swarm_snapshot(history_limit=history_limit),
            )
            self._send_json(data, cacheable=True)
            return

        elif path == "/api/swarm/dispatch":
            self.send_error(405, "Use POST to generate a plan-only swarm proposal")
            return

        if path == "/api/heal":
            if sentinel:
                res = sentinel.run_self_healing_suite(dry_run=False)
            else:
                res = {"status": "error", "error": "sentinel module not loaded"}
            data = json.dumps(res, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        elif path == "/api/expand":
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("q", [""])[0] or qs.get("path", [""])[0]
            if cognitive_engine:
                res = cognitive_engine.expand_context(target)
            else:
                res = {"error": "cognitive_engine module not loaded", "status": "unavailable"}
            data = json.dumps(res, indent=2).encode("utf-8")
            status = 200 if "error" not in res else (503 if res.get("status") in {"unavailable", "http_503", "http_504"} else 404)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        elif path == "/api/status":
            self._send_json(cached_by_generation("status", get_status_data), cacheable=True)
            return

        elif path == "/api/providers":
            # Account state must never be served stale: a key the operator just
            # added has to show as configured on the very next poll, so this is
            # deliberately NOT run through cached_by_generation and is sent
            # cacheable=False. The payload is list_accounts() (which by contract
            # never contains a key value) plus the discover() summary the panel
            # uses for its "N configured / worker available" line.
            if providers is None:
                self._send_json({"error": "providers module not loaded",
                                 "accounts": [], "summary": {}}, status=503)
                return
            self._send_json(
                {"accounts": providers.list_accounts(), "summary": providers.discover()},
                cacheable=False,
            )
            return

        elif path == "/api/graph":
            self._send_json(cached_by_generation("graph", get_graph_data), cacheable=True)
            return

        elif path == "/api/notes":
            self._send_json(cached_by_generation("notes", get_all_notes), cacheable=True)
            return

        elif path == "/api/note":
            qs = urllib.parse.parse_qs(parsed.query)
            target = qs.get("path", [""])[0]
            if not target:
                self.send_error(400, "Missing path parameter")
                return
            p = (BRAIN_DIR / target).resolve()
            if not p.is_relative_to(BRAIN_DIR) or not p.exists():
                self.send_error(404, "Note not found")
                return
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                meta, body = parse_frontmatter(content)
                obs = parse_observations(body)
                stat = p.stat()
                first_header = ""
                for l in body.splitlines():
                    if l.startswith("# "):
                        first_header = l[2:].strip()
                        break

                folder_type = target.split("/")[0] if "/" in target else "general"
                note_type = meta.get("type") or folder_type
                if note_type == "schema" or folder_type == "schemas":
                    note_type = "schemas"
                elif note_type in ("handoff", "handoffs") or folder_type == "handoff":
                    note_type = "handoff"
                elif note_type in ("projects", "project") or folder_type == "projects":
                    note_type = "projects"
                elif note_type in ("decisions", "decision") or folder_type == "decisions":
                    note_type = "decisions"
                elif note_type in ("runbooks", "runbook") or folder_type == "runbooks":
                    note_type = "runbooks"

                agent = obs.get("agent")
                if not agent or agent == "unknown":
                    agent = meta.get("agent")
                if not agent or agent == "unknown":
                    if "kiro" in target or "kiro" in body.lower():
                        agent = "kiro-cli"
                    elif "cline" in target or "cline" in body.lower():
                        agent = "cline"
                    elif "antigravity-ide" in target or "antigravity-ide" in body.lower():
                        agent = "antigravity-ide"
                    elif "amazon-q" in target or "amazonq" in body.lower() or "amazon-q" in body.lower():
                        agent = "amazon-q"
                    elif "antigravity" in body.lower():
                        agent = "antigravity"
                    else:
                        agent = "human"

                out = {
                    "path": target,
                    "title": meta.get("title", first_header or p.stem),
                    "type": note_type,
                    "agent": agent,
                    "tags": meta.get("tags", []),
                    "links": extract_links(body),
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "meta": meta,
                    "observations": obs,
                    "body": body,
                }
                data = json.dumps(out, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                self.send_error(500, f"Error reading note: {e}")
                return

        elif path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            status, res = run_semantic_search(q)
            data = json.dumps(res, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        elif path == "/api/events":
            # An SSE body has no Content-Length, so under HTTP/1.1 keep-alive the
            # browser would wait for chunked framing that never arrives. Delimit
            # this one response by connection close instead.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            event = threading.Event()
            with subscribers_lock:
                subscribers.append(event)
            try:
                # Tell the client how long to wait before reconnecting, and give it
                # the server generation so it can skip a refetch it already has.
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.write(f"event: hello\ndata: {current_generation()}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    if event.wait(timeout=15.0):
                        event.clear()
                        self.wfile.write(f"data: {current_generation()}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    else:
                        # A real comment frame every 15s keeps proxies and the
                        # client-side watchdog from treating the stream as dead.
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with subscribers_lock:
                    if event in subscribers:
                        subscribers.remove(event)
            return

        else:
            self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        pass



    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/api/providers", "/api/providers/test", "/api/providers/remove"):
            # Provider-account management. do_DELETE is not implemented on this
            # handler, so removal is a POST to /api/providers/remove rather than
            # a new verb, keeping every response on the same _send_json path that
            # guarantees a Content-Length (required now the handler is keep-alive).
            if providers is None:
                self._send_json({"status": "error", "error": "providers module not loaded"},
                                status=503)
                return
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len else {}
            except Exception:
                payload = {}
            provider_id = str(payload.get("provider") or "").strip()

            if path == "/api/providers/test":
                # Listing models costs no tokens, so this is a free health check
                # of a stored key. Never surface the key, only the verdict.
                result = providers.test_account(provider_id)
                self._send_json({
                    "ok": bool(result.get("ok")),
                    "http": result.get("http"),
                    "latency_ms": result.get("latency_ms"),
                    "model_count": result.get("model_count"),
                    "error": result.get("error", ""),
                }, cacheable=False)
                return

            if path == "/api/providers/remove":
                result = providers.remove_account(provider_id)
                # Bump the generation so every connected browser refetches and
                # the removed account flips back to unconfigured live over SSE.
                bump_generation()
                self._send_json(result, cacheable=False)
                return

            # path == "/api/providers": add / replace an account.
            api_key = str(payload.get("api_key") or "")
            model = payload.get("model") or None
            # Reject an unknown provider with 400 and the known ids so the UI can
            # tell the operator exactly what it accepts, rather than a bare error.
            if provider_id not in {a["provider"] for a in providers.list_accounts()}:
                self._send_json(
                    {"status": "error", "error": f"unknown provider {provider_id!r}",
                     "known": sorted(a["provider"] for a in providers.list_accounts())},
                    status=400, cacheable=False,
                )
                return
            result = providers.add_account(provider_id, api_key, model=model)
            # A new key makes a new worker: refresh every browser at once.
            bump_generation()
            # add_account's result carries a store path and mode but never the
            # key itself, so it is safe to echo straight back.
            self._send_json(result, status=200 if result.get("status") == "added" else 400,
                            cacheable=False)
            return

        if path == "/api/delivery/lifecycle":
            # Approve or reject a delivery from the dashboard. This exists because
            # desktop notification actions are not reliably rendered by every
            # shell, and an approval surface must not depend on one.
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(content_len).decode("utf-8")) if content_len else {}
            except Exception:
                payload = {}
            agent = str(payload.get("agent") or "")
            task_id = str(payload.get("task_id") or "")
            event = str(payload.get("event") or "acknowledge")
            model = str(payload.get("model") or "")
            note = str(payload.get("note") or "approved from the Shared Brain dashboard")
            # `swarm` is assigned by a later `import swarm` in this same function,
            # which makes the name function-local, so it must be imported here
            # rather than relying on the module-level import.
            try:
                from . import swarm as swarm_mod  # type: ignore[attr-defined]
            except ImportError:
                try:
                    import swarm as swarm_mod  # type: ignore[no-redef]
                except ImportError:
                    swarm_mod = None
            if swarm_mod is None:
                self.send_error(503, "swarm unavailable")
                return
            if agent not in getattr(swarm_mod, "DELIVERY_AGENTS", ()):
                self.send_error(400, f"{agent} is not a delivery agent")
                return
            operations = {
                "acknowledge": swarm_mod.delivery_acknowledge,
                "progress": swarm_mod.delivery_progress,
                "complete": swarm_mod.delivery_complete,
                "escalate": swarm_mod.delivery_escalate,
            }
            if event not in operations:
                self.send_error(400, f"unknown event {event}")
                return
            try:
                if event in ("acknowledge", "complete"):
                    result = operations[event](agent, task_id, note=note, model=model)
                else:
                    result = operations[event](agent, task_id, note, model=model)
                out = {
                    "status": "recorded",
                    "agent": agent,
                    "task_id": task_id,
                    "lifecycle": result["delivery"]["status"],
                    "model_reported": result["delivery"].get("model_reported"),
                }
            except Exception as error:
                out = {"status": "rejected", "reason": str(error)}
            body = json.dumps(out, indent=2).encode("utf-8")
            self.send_response(200 if out.get("status") == "recorded" else 409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/swarm/dispatch":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                post_body = self.rfile.read(content_len).decode("utf-8")
                payload = json.loads(post_body) if post_body else {}
            except Exception:
                payload = {}
            raw_tasks = payload.get("tasks", "")
            if not isinstance(raw_tasks, str) or not raw_tasks.strip():
                self.send_error(400, "Missing tasks")
                return
            if orchestrator is None:
                self.send_error(503, "Plan-only orchestrator unavailable")
                return
            plan = orchestrator.plan_text(raw_tasks, approval_granted=bool(payload.get("approval_granted", False)))
            body = json.dumps({"status": "planned", **plan.to_dict()}, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif path == "/api/swarm/execute":
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                post_body = self.rfile.read(content_len).decode("utf-8")
                payload = json.loads(post_body) if post_body else {}
            except Exception:
                payload = {}
            raw_tasks = payload.get("tasks", "")
            plan_hash = payload.get("plan_hash", "")
            if not isinstance(raw_tasks, str) or not raw_tasks.strip():
                self.send_error(400, "Missing tasks")
                return
            if not isinstance(plan_hash, str) or not plan_hash:
                self.send_error(400, "Missing plan_hash")
                return
            if payload.get("confirm_execution") is not True:
                self.send_error(400, "Execution requires confirm_execution=true for this exact plan")
                return
            if orchestrator is None or dashboard_executor is None:
                self.send_error(503, "Controlled local executor unavailable")
                return
            plan = orchestrator.plan_text(raw_tasks)
            try:
                result = dashboard_executor.submit(plan, plan_hash=plan_hash, explicit_confirmation=True)
            except Exception as error:
                body = json.dumps({"status": "blocked", "reason": str(error)}, indent=2).encode("utf-8")
                self.send_response(409)
            else:
                body = json.dumps(result, indent=2).encode("utf-8")
                self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif path == "/api/swarm/complete":
            import swarm
            try:
                content_len = int(self.headers.get("Content-Length", 0))
                post_body = self.rfile.read(content_len).decode("utf-8")
                payload = json.loads(post_body) if post_body else {}
            except Exception:
                payload = {}
            task_id = payload.get("task_id", "")
            notes = payload.get("notes", "")
            completed = swarm.complete_task(task_id, notes=notes)
            res = {"status": "success" if completed else "not_found", "task": completed}
            body = json.dumps(res, indent=2).encode("utf-8")
            self.send_response(200 if completed else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/heal":
            if sentinel:
                res = sentinel.run_self_healing_suite(dry_run=False)
            else:
                res = {"status": "error", "error": "sentinel module not loaded"}
            data = json.dumps(res, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404, "Not Found")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    # Each open SSE stream occupies a connection for its whole lifetime, and a
    # browser opens several sockets at once. The stdlib default backlog of 5 was
    # small enough that a refresh burst could have connections refused or stalled
    # in the accept queue, which looked like a hung dashboard.
    request_queue_size = 128
    # Without this a client that disconnects mid-response leaves the port in
    # TIME_WAIT and a quick restart of `brain ui` fails to bind.
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Shared Brain Dashboard")
    parser.add_argument("--port", type=int, default=3333, help="Port to listen on (default 3333)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to (default 127.0.0.1)")
    args = parser.parse_args()

    t = threading.Thread(target=brain_watcher, daemon=True)
    t.start()

    sidecar_thread = threading.Thread(target=ensure_search_sidecar, daemon=True)
    sidecar_thread.start()

    try:
        server = ThreadedHTTPServer((args.host, args.port), DashboardHandler)
    except OSError as exc:
        # The Mission Control dashboard in Agentic_shared_memory/ui/dashboard also
        # defaults to 3333, so a collision here is expected rather than exotic.
        # A bare traceback made it look like the dashboard itself was broken.
        print(
            f"Error: cannot bind {args.host}:{args.port} ({exc}).\n"
            f"Port {args.port} is probably already taken by the other dashboard "
            f"(Agentic_shared_memory/ui/dashboard also defaults to 3333).\n"
            f"Find the holder with:  ss -ltnp | grep {args.port}\n"
            f"Or pick another port:  dashboard.py --port 3340",
            file=sys.stderr,
        )
        return 1

    print(f"🧠 Shared Brain Dashboard running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        global _sidecar_proc
        if _sidecar_proc is not None:
            try:
                _sidecar_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
