"""Connect MCP servers and expose their tools through the agent tool registry."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..tools import ToolSpec
from .client import MCPClient, render_result
from .config import CONNECT_TIMEOUT, ServerConfig
from .transport import HttpTransport, StdioTransport


@dataclass
class ServerStatus:
    name: str
    transport: str
    connected: bool = False
    tools: int = 0
    error: str | None = None
    protocol_version: str | None = None
    source: str | None = None


class MCPManager:
    def __init__(self, configs, root=".", ui=None, verbose=False):
        self.configs = [cfg for cfg in configs if not cfg.disabled]
        self.root = Path(root).resolve()
        self.ui = ui
        self.verbose = verbose
        self.sessions: dict[str, MCPClient] = {}
        self.statuses: dict[str, ServerStatus] = {}
        self.tool_map: dict[str, tuple[str, str, object]] = {}
        self._by_name: dict[str, ServerConfig] = {cfg.name: cfg for cfg in self.configs}

    # -------------------------------------------------------------- connecting

    def connect_all(self, project_approver=None) -> list[ServerStatus]:
        for cfg in self.configs:
            status = ServerStatus(name=cfg.name, transport=cfg.transport, source=cfg.source)
            self.statuses[cfg.name] = status

            if cfg.project and project_approver is not None:
                try:
                    approved = bool(project_approver(cfg))
                except Exception:
                    approved = False
                if not approved:
                    status.error = "project config not approved (use --trust-project-mcp)"
                    continue

            transport = None
            try:
                transport = self._make_transport(cfg)
                transport.start()
                client = MCPClient(
                    transport,
                    timeout=cfg.timeout,
                    connect_timeout=CONNECT_TIMEOUT,
                    name=cfg.name,
                )
                client.initialize()
                client.list_tools()
                self.sessions[cfg.name] = client
                status.connected = True
                status.tools = len(client.tools)
                status.protocol_version = client.protocol_version
            except Exception as exc:
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass
                status.error = f"{type(exc).__name__}: {exc}"
        return list(self.statuses.values())

    def _make_transport(self, cfg: ServerConfig):
        if cfg.transport == "http":
            return HttpTransport(cfg.url, headers=cfg.headers, name=cfg.name)
        env = os.environ.copy()
        env.update(cfg.env)
        return StdioTransport(
            cfg.command,
            args=cfg.args,
            env=env,
            cwd=cfg.cwd,
            name=cfg.name,
        )

    # ------------------------------------------------------------- registering

    def register_tools(self, registry) -> None:
        taken = set(registry.names())
        for server, client in self.sessions.items():
            cfg = self._by_name.get(server)
            approval = cfg.approval if cfg is not None else "auto"
            for tool in client.tools:
                facing = make_tool_name(server, tool.name, taken)
                registry.register(
                    ToolSpec(
                        name=facing,
                        description=_describe(server, tool),
                        parameters=tool.input_schema or {"type": "object", "properties": {}},
                        handler=self._handler(server, tool.name, approval),
                    )
                )
                taken.add(facing)
                self.tool_map[facing] = (server, tool.name, tool)

    def _handler(self, server: str, tool: str, approval):
        def handler(ctx, arguments):
            if getattr(ctx, "dry_run", False):
                return {
                    "ok": True,
                    "dry_run": True,
                    "server": server,
                    "tool": tool,
                    "arguments": arguments or {},
                }
            policy = getattr(ctx, "permissions", None)
            if policy is not None and hasattr(policy, "allow_mcp"):
                allowed = policy.allow_mcp(
                    server,
                    tool,
                    arguments or {},
                    ui=getattr(ctx, "ui", None),
                    approval=approval,
                )
                if not allowed:
                    return {
                        "ok": False,
                        "skipped": True,
                        "error": "declined by user",
                        "server": server,
                        "tool": tool,
                    }
            return self.call_tool(server, tool, arguments or {})

        return handler

    # --------------------------------------------------------------- calling

    def call_tool(self, server: str, tool: str, arguments: dict) -> dict:
        client = self.sessions.get(server)
        if client is None:
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "error": f"MCP server '{server}' is not connected",
            }
        try:
            result = client.call_tool(tool, arguments or {})
        except Exception as exc:
            return {
                "ok": False,
                "server": server,
                "tool": tool,
                "error": f"{type(exc).__name__}: {exc}",
            }
        is_error, text = render_result(result)
        if is_error:
            payload = {
                "ok": False,
                "server": server,
                "tool": tool,
                "error": text or "MCP tool reported an error",
            }
        else:
            payload = {"ok": True, "server": server, "tool": tool, "content": text}
        if isinstance(result, dict) and "structuredContent" in result:
            payload["structured"] = result["structuredContent"]
        return payload

    # ---------------------------------------------------------------- status

    def connected_count(self) -> int:
        return len(self.sessions)

    def connected_names(self) -> list[str]:
        return list(self.sessions)

    def status_lines(self) -> list[str]:
        if not self.statuses:
            return ["no MCP servers configured"]
        lines = []
        for status in self.statuses.values():
            if status.connected:
                lines.append(
                    f"{status.name} · {status.transport} · {status.tools} tool(s) · "
                    f"{status.protocol_version or 'unknown protocol'}"
                )
            else:
                lines.append(f"{status.name} · {status.transport} · failed: {status.error}")
        return lines

    def close(self) -> None:
        for name, client in list(self.sessions.items()):
            try:
                client.transport.close()
            except Exception:
                pass
            status = self.statuses.get(name)
            if status is not None:
                status.connected = False
        self.sessions.clear()


def make_tool_name(server: str, tool: str, taken=()) -> str:
    """Return an OpenAI-safe unique function name for an MCP tool."""
    taken = set(taken)
    base = f"mcp__{_clean(server, 'server')}__{_clean(tool, 'tool')}"
    if len(base) > 64:
        digest = hashlib.sha1(f"{server}:{tool}".encode("utf-8")).hexdigest()[:8]
        base = base[:55].rstrip("_") + "_" + digest
    name = base
    index = 2
    while name in taken:
        suffix = f"_{index}"
        name = base[: 64 - len(suffix)] + suffix
        index += 1
    return name


def _clean(part: str, fallback: str, limit: int = 24) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(part)).strip("_")
    return (cleaned or fallback)[:limit]


def _describe(server: str, tool) -> str:
    text = tool.description.strip() or f"MCP tool {tool.name}"
    text += f"\n\n[MCP server: {server}]"
    if len(text) > 4000:
        text = text[:3980] + "..."
    return text
