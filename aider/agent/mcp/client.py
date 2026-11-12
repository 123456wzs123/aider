"""A small MCP client: JSON-RPC handshake, tool discovery and tool calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
MAX_TOOL_PAGES = 50


class MCPClientError(RuntimeError):
    """The server answered with a JSON-RPC error or an invalid payload."""


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)


class MCPClient:
    def __init__(self, transport, timeout=60.0, connect_timeout=30.0, name="mcp"):
        self.transport = transport
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.name = name
        self.tools: list[MCPTool] = []
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.protocol_version: str | None = None
        self._next_id = 0

    # ------------------------------------------------------------- handshake

    def initialize(self) -> dict:
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "aider-agent", "version": _client_version()},
            },
            timeout=self.connect_timeout,
        )
        version = result.get("protocolVersion") or PROTOCOL_VERSION
        self.protocol_version = str(version)
        self.transport.protocol_version = self.protocol_version
        self.server_info = result.get("serverInfo") or {}
        self.capabilities = result.get("capabilities") or {}
        self._notify("notifications/initialized", {})
        return result

    # ------------------------------------------------------------------ tools

    def list_tools(self) -> list[MCPTool]:
        tools: list[MCPTool] = []
        seen: set[str] = set()
        cursor = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout=self.connect_timeout)
            entries = result.get("tools")
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise MCPClientError(f"{self.name}: tools/list returned a non-list 'tools'")
            for entry in entries:
                tool = _parse_tool(entry)
                if tool is None or tool.name in seen:
                    continue
                seen.add(tool.name)
                tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=self.timeout,
        )
        return result

    # --------------------------------------------------------------- helpers

    def _request(self, method: str, params=None, timeout=None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        response = self.transport.call(message, self.timeout if timeout is None else timeout)
        if not isinstance(response, dict):
            raise MCPClientError(f"{self.name}: invalid JSON-RPC response")
        error = response.get("error")
        if error:
            if isinstance(error, dict):
                detail = error.get("message") or json.dumps(error, ensure_ascii=False)
                code = error.get("code")
                if code is not None:
                    detail = f"{detail} (code {code})"
            else:
                detail = str(error)
            raise MCPClientError(f"{self.name}: {method} failed: {detail}")
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params=None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.transport.notify(message)


def _parse_tool(entry) -> MCPTool | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    description = entry.get("description")
    if not isinstance(description, str):
        description = ""
    schema = entry.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {}
    elif "type" not in schema:
        schema = {"type": "object", **schema}
    return MCPTool(name=name.strip(), description=description.strip(), input_schema=schema)


def render_result(result: dict) -> tuple[bool, str]:
    """Flatten MCP content blocks into text for the model."""
    if not isinstance(result, dict):
        return True, "invalid MCP result"
    parts = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                parts.append(_render_block(block))
            elif isinstance(block, str):
                parts.append(block)
    text = "\n".join(part for part in parts if part)

    structured = result.get("structuredContent")
    if structured is not None:
        try:
            suffix = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            suffix = str(structured)
        if len(suffix) > 4000:
            suffix = suffix[:3990] + "..."
        text = (text + "\n" if text else "") + "structuredContent: " + suffix

    is_error = bool(result.get("isError"))
    return is_error, text


def _render_block(block: dict) -> str:
    kind = block.get("type")
    if kind == "text":
        return str(block.get("text") or "")
    if kind == "image":
        data = str(block.get("data") or "")
        return f"[image: {block.get('mimeType') or 'unknown'}, {len(data)} base64 chars]"
    if kind == "audio":
        data = str(block.get("data") or "")
        return f"[audio: {block.get('mimeType') or 'unknown'}, {len(data)} base64 chars]"
    if kind == "resource":
        resource = block.get("resource")
        if isinstance(resource, dict):
            if resource.get("text") is not None:
                return str(resource["text"])
            uri = resource.get("uri") or ""
            mime = resource.get("mimeType") or ""
            label = uri + (f" ({mime})" if mime else "")
            return f"[resource: {label}]"
        return f"[resource: {resource}]"
    if kind == "resource_link":
        return f"[resource link: {block.get('uri') or block.get('name') or ''}]"
    try:
        return json.dumps(block, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(block)


def _client_version() -> str:
    try:
        from aider import __version__

        return __version__
    except Exception:
        return "0"
