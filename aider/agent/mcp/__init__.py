"""MCP support for aider-agent: config discovery, transports and tool exposure."""

from .config import ServerConfig, load_server_configs
from .manager import MCPManager, ServerStatus, make_tool_name

__all__ = [
    "MCPManager",
    "ServerConfig",
    "ServerStatus",
    "load_server_configs",
    "make_tool_name",
]
