"""Automatic MCP Discovery Engine for Mission Control.

Discovers Model Context Protocol (MCP) server configurations across approved
and safe host configuration roots in a strictly READ-ONLY manner.
Never rewrites external configuration files.
Never extracts or stores plaintext credentials.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from providers.registry.mcp_registry import (
    MCPHealthStatus,
    MCPServer,
    MCPTool,
    MCPTransport,
)

logger = logging.getLogger(__name__)

# Known capability mappings for well-known MCP servers
KNOWN_MCP_CAPABILITIES: Dict[str, List[str]] = {
    "azure": [
        "azure", "cloud", "infrastructure", "deployment", "arm",
        "bicep", "terraform", "resource-management", "aks", "storage"
    ],
    "firecrawl": [
        "web-scraping", "web-crawling", "web-search", "content-extraction",
        "markdown-conversion", "research"
    ],
    "render": [
        "render", "cloud", "deployment", "web-services", "postgres",
        "hosting", "devops"
    ],
    "cloudflare": [
        "cloudflare", "dns", "workers", "kv", "cdn", "security", "cloud"
    ],
    "bitdefender": [
        "security", "endpoint-protection", "vulnerability-scan", "antivirus",
        "quarantine", "threat-detection"
    ],
    "brain": [
        "memory", "knowledge-graph", "semantic-notes", "context", "shared-memory"
    ],
    "basic-memory": [
        "memory", "knowledge-graph", "semantic-notes", "context", "shared-memory"
    ],
    "omniroute": [
        "api-routing", "proxy", "gateway", "multi-provider"
    ],
    "betterclaw": [
        "coordination", "tool-orchestration", "agentic-tasks"
    ],
    "kirocrew-core": [
        "agent-coordination", "team-orchestration", "workflow"
    ],
    "kirocrew-cron": [
        "scheduling", "cron", "automation", "timed-tasks"
    ],
    "kirocrew-computer": [
        "local-automation", "desktop-control", "shell"
    ],
    "github": [
        "git", "github", "code-search", "pull-requests", "issues", "ci-cd"
    ],
    "filesystem": [
        "file-operations", "local-filesystem", "workspace-read"
    ]
}


@dataclass
class DiscoveredMCPServer:
    """Represents an MCP server detected by the discovery engine."""
    source: str
    path: str
    server_id: str
    command: str = ""
    args: List[str] = field(default_factory=list)
    env_keys: List[str] = field(default_factory=list)
    transport: str = "stdio"
    detected_capabilities: List[str] = field(default_factory=list)
    confidence: float = 1.0
    safety_status: str = "safe"  # "safe", "review_required", "disabled"
    raw_config: Dict[str, Any] = field(default_factory=dict)

    def to_mcp_server(self) -> MCPServer:
        """Convert discovered server into a standard MCPServer instance."""
        # Determine tags from id and capabilities
        tags = [self.source]
        for cap in self.detected_capabilities:
            if cap not in tags:
                tags.append(cap)

        # Check command availability on system
        cmd_path = shutil.which(self.command) if self.command else None
        health = MCPHealthStatus.HEALTHY if cmd_path or self.command == "npx" or self.command == "node" else MCPHealthStatus.UNKNOWN

        return MCPServer(
            id=self.server_id,
            name=self.server_id.replace("-", " ").title(),
            command=self.command,
            arguments=self.args,
            env_keys=self.env_keys,
            transport=MCPTransport.STDIO if self.transport == "stdio" else MCPTransport.CUSTOM,
            enabled=True,
            health=health,
            source=self.source,
            source_path=self.path,
            capabilities=list(self.detected_capabilities),
            tags=tags,
            description=f"Auto-discovered from {self.source} ({Path(self.path).name})"
        )


class MCPDiscoveryEngine:
    """Discovers MCP configurations across approved search paths."""

    def __init__(self, search_paths: Optional[List[str]] = None) -> None:
        self.search_paths = search_paths or self._get_default_search_paths()

    def _get_default_search_paths(self) -> List[str]:
        """Collect approved, safe paths to inspect for MCP configurations."""
        home = os.path.expanduser("~")
        candidates = [
            # 1. Project-local configurations
            os.path.join(".", "config", "mcp.json"),
            os.path.join(".", "mcp", "configs"),
            # 2. Cline MCP configuration
            os.path.join(home, ".cline", "data", "settings", "cline_mcp_settings.json"),
            os.path.join(home, ".config", "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
            # 3. VS Code user MCP configuration
            os.path.join(home, ".config", "Code", "User", "mcp.json"),
            # 4. Kiro / Agentic OS configurations
            os.path.join(os.environ.get("BRAIN_WORKSPACE_ROOT", os.getcwd()), ".agents", "plugins", "kiro-mcp", "mcp_config.json"),
            os.path.join(os.environ.get("BRAIN_WORKSPACE_ROOT", os.getcwd()), ".kiro", "settings", "mcp.json"),
            os.path.join(home, ".kiro", "settings", "mcp.json"),
            os.path.join(".", ".mcp.json"),
            os.path.join(".", "mcp.json"),
            os.path.join(home, ".gemini", "antigravity-cli", "mcp"),
        ]
        return candidates

    def discover(self) -> List[DiscoveredMCPServer]:
        """Scan all candidate locations and return discovered MCP servers."""
        discovered: Dict[str, DiscoveredMCPServer] = {}

        for target in self.search_paths:
            path = Path(os.path.expanduser(target))
            if not path.exists():
                continue

            if path.is_file():
                self._parse_mcp_file(path, discovered)
            elif path.is_dir():
                for sub in path.glob("*.json"):
                    self._parse_mcp_file(sub, discovered)
                # Check for subdirectories representing MCP servers (e.g. antigravity-cli/mcp/<server>)
                for sub_dir in path.iterdir():
                    if sub_dir.is_dir() and not sub_dir.name.startswith("."):
                        s_id = sub_dir.name.lower().replace(" ", "-")
                        if s_id not in discovered:
                            caps = list(KNOWN_MCP_CAPABILITIES.get(s_id, [s_id]))
                            discovered[s_id] = DiscoveredMCPServer(
                                source="antigravity",
                                path=str(sub_dir),
                                server_id=s_id,
                                command="mcp",
                                args=[],
                                env_keys=[],
                                transport="stdio",
                                detected_capabilities=caps,
                                confidence=1.0,
                                safety_status="safe",
                                raw_config={"source": "antigravity-cli/mcp"}
                            )

        return list(discovered.values())

    def _parse_mcp_file(self, path: Path, out: Dict[str, DiscoveredMCPServer]) -> None:
        """Parse a single JSON MCP configuration file safely."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
        except Exception as e:
            logger.debug("Skipping unparseable MCP file %s: %s", path, e)
            return

        if not isinstance(data, dict):
            return

        # Determine source classifier
        p_str = str(path)
        if "cline" in p_str.lower():
            source = "cline"
        elif "kiro" in p_str.lower():
            source = "kiro"
        elif "agentic_os" in p_str.lower():
            source = "agentic_os"
        elif "code" in p_str.lower():
            source = "vscode"
        else:
            source = "project"

        # Schema variations:
        # Standard: {"mcpServers": { "server_name": { "command": ..., "args": ... } }}
        # Alternate: {"servers": { ... }}
        # Flat dict: { "server_name": { "command": ..., "args": ... } }
        servers_block = data.get("mcpServers") or data.get("servers") or data

        if not isinstance(servers_block, dict):
            return

        for server_name, server_cfg in servers_block.items():
            if not isinstance(server_cfg, dict):
                continue

            cmd = server_cfg.get("command", "")
            args = server_cfg.get("args", [])
            if not isinstance(args, list):
                args = [str(args)]
            args = [str(a) for a in args]

            # Extract env variable NAMES only, never values
            env_block = server_cfg.get("env", {})
            env_keys = list(env_block.keys()) if isinstance(env_block, dict) else []

            # Infer capabilities from server_name and arguments
            caps = list(KNOWN_MCP_CAPABILITIES.get(server_name.lower(), []))
            for arg in args:
                for k, known_caps in KNOWN_MCP_CAPABILITIES.items():
                    if k in arg.lower():
                        for c in known_caps:
                            if c not in caps:
                                caps.append(c)

            if not caps:
                caps = [server_name.lower()]

            # Determine safety status
            safety = "safe"
            if any(term in cmd.lower() or any(term in a.lower() for a in args) for term in ["rm", "delete", "destroy", "format"]):
                safety = "review_required"

            server_id = server_name.lower().replace(" ", "-")

            # Avoid overwriting a project/cline source with an identical lower-priority discovery
            if server_id not in out or source in ("cline", "project"):
                out[server_id] = DiscoveredMCPServer(
                    source=source,
                    path=str(path),
                    server_id=server_id,
                    command=cmd,
                    args=args,
                    env_keys=env_keys,
                    transport="stdio",
                    detected_capabilities=caps,
                    confidence=1.0,
                    safety_status=safety,
                    raw_config={
                        "command": cmd,
                        "args": args,
                        "env_keys": env_keys
                    }
                )
