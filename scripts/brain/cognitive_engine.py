#!/usr/bin/env python3
"""Provenance-preserving graph context retrieval for the Shared Brain.

This module never substitutes ``handoff/current`` for an unresolved request and
never writes archive/decision files.  It returns an explicit resolution mode,
canonical source path, store revision, and any retrieval warning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", Path.home() / "agentic-brain")).resolve()
SIDECAR_URL = os.environ.get("BRAIN_SIDECAR_URL", "http://127.0.0.1:3334")
WIKILINK_RE = re.compile(r"\[\[([^\|\]]+)(?:\|([^\]]+))?\]\]")
MAX_NEIGHBORS = 12
MAX_TOTAL_NODES = 24


def parse_frontmatter_and_body(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            metadata[key.strip().lower()] = value.strip().strip("\"'")
    return metadata, parts[2]


def parse_observations(body: str) -> dict[str, list[str]]:
    observations: dict[str, list[str]] = defaultdict(list)
    for line in body.splitlines():
        match = re.match(r"^\s*-\s*\[([\w-]+)\]\s*(.*)$", line)
        if match:
            observations[match.group(1).lower()].append(match.group(2).strip())
    return dict(observations)


def extract_links(content: str) -> list[str]:
    return [match.group(1).strip() for match in WIKILINK_RE.finditer(content)]


class NoteIndex:
    """Snapshot index with canonical paths and ambiguity-aware aliases."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.canonical: dict[str, Path] = {}
        self.aliases: dict[str, list[Path]] = defaultdict(list)
        self.generation_hasher = hashlib.sha256()
        if root.exists():
            for path in sorted(root.rglob("*.md")):
                if ".git" in path.parts:
                    continue
                try:
                    relative = path.relative_to(root).with_suffix("").as_posix()
                    stat = path.stat()
                except OSError:
                    continue
                key = relative.lower()
                self.canonical[key] = path
                for alias in {key, path.stem.lower()}:
                    self.aliases[alias].append(path)
                self.generation_hasher.update(f"{relative}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode())

    @property
    def generation(self) -> str:
        return self.generation_hasher.hexdigest()[:16]

    def resolve(self, identifier: str) -> tuple[Path | None, str | None]:
        clean = identifier.strip().removeprefix("brain/").removesuffix(".md").lower()
        if not clean:
            return None, "empty target"
        if "/" in clean:
            return self.canonical.get(clean), None
        if clean in self.canonical:
            return self.canonical[clean], None
        aliases = self.aliases.get(clean, [])
        if len(aliases) == 1:
            return aliases[0], None
        if len(aliases) > 1:
            names = ", ".join(sorted(p.relative_to(self.root).as_posix() for p in aliases))
            return None, f"ambiguous target {identifier!r}: {names}"
        for directory in ("handoff", "projects", "decisions", "runbooks"):
            candidate = f"{directory}/{clean}"
            if candidate in self.canonical:
                return self.canonical[candidate], None
        return None, None


def _sidecar_search(query: str) -> tuple[list[dict], dict]:
    url = f"{SIDECAR_URL}/search?{urllib.parse.urlencode({'q': query, 'page_size': 5})}"
    try:
        with urllib.request.urlopen(url, timeout=8.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("results", []), {"status": "ok", "request_id": payload.get("request_id"), "retrieval": payload.get("retrieval", {})}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        return [], {"status": f"http_{error.code}", "error": payload.get("error", f"sidecar returned {error.code}")}
    except (urllib.error.URLError, TimeoutError) as error:
        return [], {"status": "unavailable", "error": str(error)}


def _path_from_hit(hit: dict, index: NoteIndex) -> Path | None:
    """Accept only canonical filesystem/path identities from a semantic hit."""
    for key in ("file_path", "path"):
        value = hit.get(key)
        if isinstance(value, str):
            candidate = Path(value)
            if candidate.is_absolute():
                try:
                    candidate.relative_to(index.root)
                    if candidate.exists():
                        return candidate
                except ValueError:
                    pass
            resolved, warning = index.resolve(value)
            if resolved and not warning:
                return resolved
    permalink = hit.get("permalink")
    if isinstance(permalink, str):
        resolved, warning = index.resolve(permalink)
        if resolved and not warning:
            return resolved
    return None


def _node(path: Path, index: NoteIndex, include_links: bool = False) -> dict:
    content = path.read_text(encoding="utf-8", errors="replace")
    metadata, body = parse_frontmatter_and_body(content)
    relative = path.relative_to(index.root).with_suffix("").as_posix()
    node = {
        "path": relative,
        "title": metadata.get("title", path.stem),
        "type": metadata.get("type", relative.split("/")[0]),
        "observations": parse_observations(body),
    }
    if include_links:
        node["links"] = extract_links(content)
    return node


def expand_context(target_query: str) -> dict:
    """Build an explicit, bounded two-hop context dossier without hidden fallback."""
    index = NoteIndex(BRAIN_DIR)
    requested = target_query.strip() or "current"
    focal_path, warning = index.resolve(requested)
    provenance: dict = {"requested": requested, "index_generation": index.generation, "mode": "exact", "warning": warning}
    semantic_hits: list[dict] = []
    if focal_path is None and warning is None:
        semantic_hits, search_state = _sidecar_search(requested)
        provenance.update(search_state)
        for hit in semantic_hits:
            candidate = _path_from_hit(hit, index)
            if candidate is not None:
                focal_path = candidate
                provenance["mode"] = "semantic"
                provenance["semantic_score"] = hit.get("score") or hit.get("similarity")
                break
    if focal_path is None:
        status = "ambiguous" if warning else provenance.get("status", "not_found")
        return {"error": warning or f"Unable to resolve note context for {requested!r}", "status": status, "provenance": provenance, "neural_correlations": semantic_hits[:3]}

    focal = _node(focal_path, index, include_links=True)
    hop1: dict[str, dict] = {}
    for link in focal["links"]:
        path, link_warning = index.resolve(link)
        if path is not None and path != focal_path and len(hop1) < MAX_NEIGHBORS:
            node = _node(path, index, include_links=True)
            hop1[node["path"]] = node
    hop2: dict[str, dict] = {}
    for parent in hop1.values():
        for link in parent.get("links", []):
            path, _ = index.resolve(link)
            if path is None or path == focal_path:
                continue
            node = _node(path, index)
            if node["path"] not in hop1 and node["path"] not in hop2 and len(hop1) + len(hop2) < MAX_TOTAL_NODES:
                hop2[node["path"]] = node
    for node in hop1.values():
        node.pop("links", None)
    focal.pop("links", None)
    observations = focal["observations"]
    focal.update({
        "status": observations.get("status", ["in-progress"])[0],
        "agent": observations.get("agent", ["unknown"])[0],
        "scope": observations.get("scope", ["unknown"])[0],
        "next_action": observations.get("next", ["None specified"])[0],
        "blockers": observations.get("blocker", ["none"]),
        "decisions": observations.get("decision", []),
        "done": observations.get("done", []),
    })
    return {
        "focal_node": focal,
        "first_degree_neighbors": list(hop1.values()),
        "second_degree_neighbors": list(hop2.values()),
        "neural_correlations": semantic_hits[:3],
        "provenance": provenance,
        "truncated": len(hop1) >= MAX_NEIGHBORS or len(hop1) + len(hop2) >= MAX_TOTAL_NODES,
    }


def format_dossier_markdown(dossier: dict) -> str:
    if "error" in dossier:
        return f"# Context unavailable\n\n- **Error**: {dossier['error']}\n- **Status**: {dossier.get('status', 'unknown')}"
    focal = dossier["focal_node"]
    lines = [f"# Cognitive Dossier: {focal['title']} (`{focal['path']}`)", "", "## Provenance", f"- Mode: `{dossier['provenance']['mode']}`", f"- Index generation: `{dossier['provenance']['index_generation']}`", "", "## Executive Operational Status", f"- **Status**: `{focal['status'].upper()}`", f"- **Assigned Agent**: `{focal['agent']}`", f"- **Immediate Next Action**: {focal['next_action']}"]
    return "\n".join(lines)


def distill_completed_handoffs(dry_run: bool = False) -> list[str]:
    """Return only a lifecycle planning notice; automatic distillation is disabled."""
    return ["No mutation performed: use lifecycle.build_archive_plan and explicit confirmation before archiving."]


def main() -> None:
    parser = argparse.ArgumentParser(description="Provenance-preserving Brain context expander")
    parser.add_argument("query", nargs="?", default="current")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--distill", action="store_true", help="show non-mutating lifecycle guidance")
    args = parser.parse_args()
    if args.distill:
        print("\n".join(distill_completed_handoffs()))
        return
    dossier = expand_context(args.query)
    if args.json:
        print(json.dumps(dossier, indent=2))
    else:
        print(format_dossier_markdown(dossier))
    if "error" in dossier:
        sys.exit(1)


if __name__ == "__main__":
    main()
