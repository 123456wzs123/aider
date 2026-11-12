import contextlib
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from aider.agent.cli import parse_args
from aider.agent.mcp import MCPManager, ServerConfig, load_server_configs, make_tool_name
from aider.agent.mcp.client import render_result
from aider.agent.permissions import PermissionPolicy
from aider.agent.prompt import build_system_prompt
from aider.agent.tools import ToolContext, build_registry
from aider.agent.ui import AgentUI

FAKE_SERVER = r"""
import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "image",
        "description": "Returns an image block",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "structured",
        "description": "Returns structured content",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "Sleeps before answering",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    },
    {
        "name": "crash",
        "description": "Exits the process",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
            }})
        elif method == "notifications/initialized":
            sys.stderr.write("initialized\n")
            sys.stderr.flush()
        elif method == "tools/list":
            if params.get("cursor"):
                send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS[:1]}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "tools": TOOLS[:4],
                    "nextCursor": "page2",
                }})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "info", "data": "busy"}})
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": "echo: " + str(arguments.get("text", ""))}
                ], "isError": False}})
            elif name == "fail":
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": "bad input"}
                ], "isError": True}})
            elif name == "image":
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "image", "mimeType": "image/png", "data": "AAAA"},
                    {"type": "text", "text": "caption"},
                ]}})
            elif name == "structured":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "done"}],
                    "structuredContent": {"answer": 42},
                }})
            elif name == "slow":
                time.sleep(float(arguments.get("seconds", 1)))
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [
                    {"type": "text", "text": "slow done"}
                ]}})
            elif name == "crash":
                sys.stderr.write("crashing on purpose\n")
                sys.stderr.flush()
                os._exit(3)
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": "unknown tool",
                }})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": "method not found",
            }})


main()
"""

ECHO_TOOL = {
    "name": "echo",
    "description": "Echo text back",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


class StdioCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_mcp.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")

    def start(self, **overrides):
        data = {
            "name": "fake",
            "command": sys.executable,
            "args": ["-u", str(self.script)],
            "timeout": 5.0,
        }
        data.update(overrides)
        manager = MCPManager([ServerConfig(**data)], root=self.root)
        self.addCleanup(manager.close)
        manager.connect_all()
        self.assertEqual(manager.connected_count(), 1, manager.status_lines())
        return manager


class TestStdioTransport(StdioCase):
    def test_connect_and_paginated_tools(self):
        manager = self.start()

        status = manager.statuses["fake"]
        self.assertTrue(status.connected)
        self.assertEqual(status.tools, 4)  # page 2 repeated echo and was deduped
        self.assertEqual(status.protocol_version, "2025-06-18")
        self.assertIn("4 tool(s)", manager.status_lines()[0])

    def test_call_text_image_structured_and_errors(self):
        manager = self.start()

        echo = manager.call_tool("fake", "echo", {"text": "hi"})
        self.assertTrue(echo["ok"])
        self.assertEqual(echo["content"], "echo: hi")

        image = manager.call_tool("fake", "image", {})
        self.assertTrue(image["ok"])
        self.assertIn("[image: image/png, 4 base64 chars]", image["content"])
        self.assertIn("caption", image["content"])

        structured = manager.call_tool("fake", "structured", {})
        self.assertTrue(structured["ok"])
        self.assertEqual(structured["structured"], {"answer": 42})
        self.assertIn("structuredContent", structured["content"])

        failed = manager.call_tool("fake", "fail", {})
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error"], "bad input")
        self.assertNotIn("content", failed)

        unknown = manager.call_tool("fake", "nope", {})
        self.assertFalse(unknown["ok"])
        self.assertIn("unknown tool", unknown["error"])

    def test_timeout_then_recovery(self):
        manager = self.start(timeout=0.7)

        slow = manager.call_tool("fake", "slow", {"seconds": 1.3})
        self.assertFalse(slow["ok"])
        self.assertIn("did not answer within", slow["error"])

        time.sleep(1.3)  # let the in-flight response arrive and be discarded
        recovered = manager.call_tool("fake", "echo", {"text": "again"})
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["content"], "echo: again")

    def test_crash_reports_exit_code_and_stderr(self):
        manager = self.start()

        crashed = manager.call_tool("fake", "crash", {})
        self.assertFalse(crashed["ok"])
        self.assertIn("code 3", crashed["error"])
        self.assertIn("crashing on purpose", crashed["error"])

    def test_close_terminates_process_and_blocks_calls(self):
        manager = self.start()
        transport = manager.sessions["fake"].transport

        manager.close()

        self.assertIsNotNone(transport._proc.poll())
        self.assertEqual(manager.connected_count(), 0)
        payload = manager.call_tool("fake", "echo", {"text": "x"})
        self.assertFalse(payload["ok"])
        self.assertIn("not connected", payload["error"])

    def test_registry_registration_and_permissions(self):
        manager = self.start(approval="ask")
        registry = build_registry()
        manager.register_tools(registry)

        names = registry.names()
        self.assertIn("mcp__fake__echo", names)
        self.assertIn("mcp__fake__structured", names)
        schema = next(s for s in registry.schemas() if s["function"]["name"] == "mcp__fake__echo")
        self.assertEqual(schema["function"]["parameters"]["required"], ["text"])
        self.assertIn("MCP server: fake", schema["function"]["description"])

        denied = ToolContext(
            root=self.root,
            permissions=PermissionPolicy(mcp_approver=lambda server, tool, args: "n"),
        )
        payload = json.loads(registry.dispatch("mcp__fake__echo", {"text": "x"}, denied))
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["skipped"])

        allowed = ToolContext(
            root=self.root,
            permissions=PermissionPolicy(mcp_approver=lambda server, tool, args: "y"),
        )
        payload = json.loads(registry.dispatch("mcp__fake__echo", {"text": "x"}, allowed))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["content"], "echo: x")

        calls = []

        def once(server, tool, args):
            calls.append((server, tool))
            return "a"

        remember = ToolContext(root=self.root, permissions=PermissionPolicy(mcp_approver=once))
        json.loads(registry.dispatch("mcp__fake__echo", {"text": "1"}, remember))
        json.loads(registry.dispatch("mcp__fake__echo", {"text": "2"}, remember))
        self.assertEqual(len(calls), 1)

    def test_dry_run_does_not_call_server(self):
        manager = self.start()
        registry = build_registry()
        manager.register_tools(registry)

        context = ToolContext(root=self.root, dry_run=True)
        payload = json.loads(registry.dispatch("mcp__fake__echo", {"text": "x"}, context))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertNotIn("content", payload)

    def test_default_approval_is_auto(self):
        manager = self.start()
        registry = build_registry()
        manager.register_tools(registry)

        context = ToolContext(root=self.root, permissions=PermissionPolicy())
        payload = json.loads(registry.dispatch("mcp__fake__echo", {"text": "ok"}, context))
        self.assertTrue(payload["ok"])

    def test_project_server_requires_approval(self):
        cfg = ServerConfig(
            name="fake",
            command=sys.executable,
            args=["-u", str(self.script)],
            timeout=5.0,
            project=True,
        )
        manager = MCPManager([cfg], root=self.root)
        self.addCleanup(manager.close)

        statuses = manager.connect_all(project_approver=lambda config: False)
        self.assertFalse(statuses[0].connected)
        self.assertIn("not approved", statuses[0].error)

        manager = MCPManager([cfg], root=self.root)
        self.addCleanup(manager.close)
        statuses = manager.connect_all(project_approver=lambda config: True)
        self.assertTrue(statuses[0].connected)

    def test_make_tool_name(self):
        self.assertEqual(make_tool_name("my server!", "do/thing"), "mcp__my_server__do_thing")
        base = make_tool_name("srv", "tool")
        self.assertEqual(make_tool_name("srv", "tool", {base}), base + "_2")
        long_name = make_tool_name("s" * 100, "t" * 100)
        self.assertLessEqual(len(long_name), 64)
        self.assertTrue(long_name.startswith("mcp__"))

    def test_render_result_blocks(self):
        ok, text = render_result(
            {
                "content": [
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///a",
                            "mimeType": "text/plain",
                            "text": "resource text",
                        },
                    },
                    {"type": "resource_link", "uri": "file:///b"},
                ]
            }
        )
        self.assertFalse(ok)
        self.assertIn("resource text", text)
        self.assertIn("[resource link: file:///b]", text)

        is_error, text = render_result(
            {"content": [{"type": "text", "text": "boom"}], "isError": True}
        )
        self.assertTrue(is_error)
        self.assertEqual(text, "boom")


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "repo"
        self.root.mkdir()
        self.home = base / "home"
        self.home.mkdir()

    def write(self, path, servers):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")

    def test_discovery_order_and_override(self):
        self.write(self.home / ".aider-agent" / "mcp.json", {"a": {"command": "home-a"}})
        self.write(
            self.root / ".aider-agent" / "mcp.json",
            {"a": {"command": "project-a"}, "b": {"command": "project-b"}},
        )
        self.write(self.root / ".mcp.json", {"b": {"command": "root-b"}})
        extra = Path(self.tmp.name) / "extra.json"
        self.write(extra, {"a": {"command": "extra-a"}})

        configs, warnings = load_server_configs(
            self.root,
            extra_paths=[str(extra)],
            home_dir=self.home,
            environ={},
        )

        self.assertEqual([cfg.name for cfg in configs], ["a", "b"])
        by_name = {cfg.name: cfg for cfg in configs}
        self.assertEqual(by_name["a"].command, "extra-a")
        self.assertFalse(by_name["a"].project)
        self.assertEqual(by_name["b"].command, "root-b")
        self.assertTrue(by_name["b"].project)
        self.assertEqual(warnings, [])

    def test_env_expansion_and_missing_vars(self):
        self.write(
            self.home / ".aider-agent" / "mcp.json",
            {
                "s": {
                    "command": "run",
                    "args": ["--key", "${MCP_KEY}", "%MCP_TOKEN%"],
                    "env": {"TOKEN": "$MCP_TOKEN"},
                    "headers": {"X-Key": "${MCP_KEY}"},
                }
            },
        )

        configs, warnings = load_server_configs(
            self.root,
            home_dir=self.home,
            environ={"MCP_KEY": "k1", "MCP_TOKEN": "t1"},
        )
        self.assertEqual(warnings, [])
        cfg = configs[0]
        self.assertEqual(cfg.args, ["--key", "k1", "t1"])
        self.assertEqual(cfg.env["TOKEN"], "t1")
        self.assertEqual(cfg.headers["X-Key"], "k1")

        configs, warnings = load_server_configs(self.root, home_dir=self.home, environ={})
        self.assertEqual(configs[0].args, ["--key", "${MCP_KEY}", "%MCP_TOKEN%"])
        self.assertTrue(any("MCP_KEY" in warning for warning in warnings))
        self.assertTrue(any("MCP_TOKEN" in warning for warning in warnings))

    def test_invalid_entries_and_disabled(self):
        self.write(
            self.home / ".aider-agent" / "mcp.json",
            {
                "empty": {},
                "both": {"command": "x", "url": "http://example.com"},
                "badtimeout": {"command": "x", "timeout": 0},
                "badargs": {"command": "x", "args": "nope"},
                "badapproval": {"command": "x", "approval": 5},
                "disabled": {"command": "x", "disabled": True},
            },
        )

        configs, warnings = load_server_configs(self.root, home_dir=self.home, environ={})

        self.assertEqual([cfg.name for cfg in configs], ["badapproval", "disabled"])
        self.assertEqual(configs[0].approval, "auto")
        self.assertTrue(configs[1].disabled)
        text = "\n".join(warnings)
        self.assertIn("needs 'command' or 'url'", text)
        self.assertIn("either 'command' or 'url'", text)
        self.assertIn("'timeout' must be positive", text)
        self.assertIn("'args' must be a list of strings", text)
        self.assertIn("'approval' must be", text)

        manager = MCPManager(configs, root=self.root)
        self.assertEqual([cfg.name for cfg in manager.configs], ["badapproval"])

    def test_cwd_resolves_against_config_file(self):
        self.write(
            self.root / ".aider-agent" / "mcp.json",
            {"s": {"command": "x", "cwd": "servers"}},
        )

        configs, _ = load_server_configs(self.root, home_dir=self.home, environ={})
        self.assertEqual(Path(configs[0].cwd), (self.root / ".aider-agent" / "servers").resolve())

    def test_target_hides_url_query(self):
        cfg = ServerConfig(name="remote", url="https://mcp.example.com/v1?api_key=secret#f")
        self.assertEqual(cfg.target(), "https://mcp.example.com/v1")
        self.assertEqual(cfg.transport, "http")
        local = ServerConfig(name="local", command="npx", args=["-y", "pkg"])
        self.assertEqual(local.target(), "npx -y pkg")

    def test_invalid_json(self):
        path = self.home / ".aider-agent" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")

        configs, warnings = load_server_configs(self.root, home_dir=self.home, environ={})
        self.assertEqual(configs, [])
        self.assertTrue(any("invalid JSON" in warning for warning in warnings))


def _make_http_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _json(self, payload, status=200, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _empty(self, status=202):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self):
            state["deleted"] = True
            self._empty(200)

        def do_POST(self):
            message = self._body()
            headers = {key.lower(): value for key, value in self.headers.items()}
            state.setdefault("requests", []).append(
                {"method": message.get("method"), "headers": headers}
            )
            if self.path != "/mcp":
                self._json({"error": {"message": "not found"}}, status=404)
                return
            method = message.get("method")
            mid = message.get("id")
            if method == "initialize":
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "http-fake", "version": "1"},
                        },
                    },
                    headers={"Mcp-Session-Id": "sess-1"},
                )
            elif method == "notifications/initialized":
                self._empty()
            elif method == "tools/list":
                self._json({"jsonrpc": "2.0", "id": mid, "result": {"tools": [ECHO_TOOL]}})
            elif method == "tools/call":
                if state.get("fail_calls"):
                    self._json({"error": {"message": "boom"}}, status=500)
                    return
                name = (message.get("params") or {}).get("name")
                if name != "echo":
                    self._json(
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "error": {"code": -32601, "message": "no such tool"},
                        }
                    )
                    return
                if state.get("sse"):
                    response = {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {"content": [{"type": "text", "text": "sse: ok"}]},
                    }
                    body = (
                        "event: message\r\n"
                        'data: {"jsonrpc":"2.0","method":"notifications/message",'
                        '"params":{"level":"info"}}\r\n'
                        "\r\n"
                        "event: message\r\n"
                        "data: " + json.dumps(response) + "\r\n\r\n"
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "result": {"content": [{"type": "text", "text": "http: ok"}]},
                    }
                )
            elif mid is not None:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )

    return Handler


class TestHttpTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_http_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/mcp"

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def make_manager(self, url=None, **overrides):
        data = {"name": "web", "url": url or self.url, "timeout": 5.0}
        data.update(overrides)
        manager = MCPManager([ServerConfig(**data)], root=self.root)
        self.addCleanup(manager.close)
        return manager

    def test_connect_call_and_session_headers(self):
        manager = self.make_manager()
        manager.connect_all()
        self.assertTrue(manager.statuses["web"].connected)

        payload = manager.call_tool("web", "echo", {"text": "hi"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["content"], "http: ok")

        requests = self.state["requests"]
        init = next(item for item in requests if item["method"] == "initialize")
        self.assertIsNone(init["headers"].get("mcp-protocol-version"))
        listing = next(item for item in requests if item["method"] == "tools/list")
        self.assertEqual(listing["headers"].get("mcp-session-id"), "sess-1")
        self.assertEqual(listing["headers"].get("mcp-protocol-version"), "2025-06-18")

    def test_sse_response(self):
        self.state["sse"] = True
        manager = self.make_manager()
        manager.connect_all()

        payload = manager.call_tool("web", "echo", {"text": "hi"})
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["content"], "sse: ok")

    def test_connect_error_is_reported(self):
        manager = self.make_manager(url=self.url.replace("/mcp", "/missing"))
        statuses = manager.connect_all()
        self.assertFalse(statuses[0].connected)
        self.assertIn("404", statuses[0].error)

    def test_call_error_is_reported(self):
        manager = self.make_manager()
        manager.connect_all()
        self.state["fail_calls"] = True

        payload = manager.call_tool("web", "echo", {"text": "hi"})
        self.assertFalse(payload["ok"])
        self.assertIn("500", payload["error"])
        self.assertIn("boom", payload["error"])

    def test_close_ends_the_session(self):
        manager = self.make_manager()
        manager.connect_all()
        manager.close()
        self.assertTrue(self.state.get("deleted"))


class TestUiPromptAndCli(unittest.TestCase):
    def test_pretty_name(self):
        ui = AgentUI(plain=False, ascii_only=False)
        self.assertEqual(ui.pretty_name("mcp__files__read_file"), "files · read_file")
        self.assertEqual(ui.pretty_name("read_file"), "read_file")
        ascii_ui = AgentUI(plain=True, ascii_only=True)
        self.assertEqual(ascii_ui.pretty_name("mcp__files__read_file"), "files - read_file")

    def test_plain_mcp_result(self):
        ui = AgentUI(plain=True, ascii_only=True)
        ui.tool_call("mcp__demo__echo", {"text": "hi"})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ui.tool_result(
                "mcp__demo__echo",
                {"ok": True, "content": "line1\nline2", "server": "demo", "tool": "echo"},
                0.1,
            )
        output = buffer.getvalue()
        self.assertIn("demo - echo", output)
        self.assertIn('{"text": "hi"}', output)
        self.assertIn("line1", output)

    def test_confirm_tool_reads_input(self):
        ui = AgentUI(plain=True)
        with mock.patch("builtins.input", return_value="y"):
            self.assertEqual(ui.confirm_tool("demo", "echo", {"text": "hi"}), "y")

    def test_prompt_mentions_mcp(self):
        text = build_system_prompt(Path.cwd(), mcp_servers=["files", "web"])
        self.assertIn("files, web", text)
        self.assertIn("mcp__<server>__<tool>", text)
        self.assertNotIn("MCP tools", build_system_prompt(Path.cwd()))

    def test_cli_flags(self):
        args = parse_args(
            [
                "--no-mcp",
                "--ask-mcp",
                "--trust-project-mcp",
                "--mcp-config",
                "a.json",
                "--mcp-config",
                "b.json",
            ]
        )
        self.assertTrue(args.no_mcp)
        self.assertTrue(args.ask_mcp)
        self.assertTrue(args.trust_project_mcp)
        self.assertEqual(args.mcp_config, ["a.json", "b.json"])

        defaults = parse_args([])
        self.assertFalse(defaults.no_mcp)
        self.assertEqual(defaults.mcp_config, [])
