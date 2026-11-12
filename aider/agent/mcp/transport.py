"""JSON-RPC transports for MCP: a stdio subprocess and Streamable HTTP."""

from __future__ import annotations

import json
import locale
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
STDERR_LINES = 60


class MCPTransportError(RuntimeError):
    """A transport-level failure (spawn, pipe, HTTP, timeout)."""


_EOF = object()


class StdioTransport:
    def __init__(self, command, args=None, env=None, cwd=None, name="mcp"):
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self.name = name
        self._proc = None
        self._inbox: queue.Queue = queue.Queue()
        self._write_lock = threading.Lock()
        self._stderr: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader = None
        self._err_reader = None
        self._closed = False

    # ---------------------------------------------------------------- startup

    def start(self) -> None:
        if self._proc is not None:
            return
        argv, use_shell = self._argv()
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": use_shell,
            "cwd": self.cwd,
            "env": self.env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(argv, **kwargs)
        except FileNotFoundError:
            raise MCPTransportError(f"MCP command not found: {self.command}") from None
        except OSError as exc:
            raise MCPTransportError(f"cannot start MCP command {self.command}: {exc}") from None

        self._reader = threading.Thread(
            target=self._read_stdout, daemon=True, name=f"mcp-out-{self.name}"
        )
        self._err_reader = threading.Thread(
            target=self._read_stderr, daemon=True, name=f"mcp-err-{self.name}"
        )
        self._reader.start()
        self._err_reader.start()

    def _argv(self):
        command = self.command
        if os.name == "nt":
            resolved = shutil.which(command) or command
            if Path(resolved).suffix.lower() in (".cmd", ".bat"):
                # CreateProcess cannot run .cmd/.bat directly and cmd.exe does
                # not understand backslash-escaped quotes, so hand cmd a plain
                # command line via shell=True (npx/uvx live here on Windows).
                inner = " ".join(_cmd_quote(part) for part in (resolved, *self.args))
                return inner, True
            return [resolved, *self.args], False
        return [command, *self.args], False

    # ------------------------------------------------------------------- io

    def _read_stdout(self) -> None:
        proc = self._proc
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if len(line) > MAX_MESSAGE_BYTES:
                    self._note_stderr("[protocol] oversized message ignored")
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self._note_stderr(f"[protocol] non-JSON output: {line[:200]}")
                    continue
                self._inbox.put(payload)
        except (OSError, ValueError):
            pass
        finally:
            # Give the stderr reader a moment so exit errors carry the server's
            # own message instead of an empty tail.
            if self._err_reader is not None and self._err_reader is not threading.current_thread():
                self._err_reader.join(timeout=0.5)
            self._inbox.put(_EOF)

    def _read_stderr(self) -> None:
        proc = self._proc
        try:
            for raw in proc.stderr:
                self._note_stderr(_decode_stderr(raw).rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def _note_stderr(self, line: str) -> None:
        if not line:
            return
        with self._stderr_lock:
            self._stderr.append(line[:2000])
            del self._stderr[:-STDERR_LINES]

    def stderr_tail(self, limit=1200) -> str:
        with self._stderr_lock:
            text = "\n".join(self._stderr)
        return text[-limit:] if text else ""

    # ------------------------------------------------------------------ send

    def send(self, message: dict) -> None:
        self._check_open()
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise MCPTransportError(self._exit_message()) from None

    def notify(self, message: dict) -> None:
        self.send(message)

    def call(self, message: dict, timeout: float) -> dict:
        self.send(message)
        deadline = time.monotonic() + max(0.1, float(timeout))
        wanted = message.get("id")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPTransportError(self._timeout_message(timeout))
            try:
                item = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise MCPTransportError(self._timeout_message(timeout)) from None
            if item is _EOF:
                raise MCPTransportError(self._exit_message())
            if not isinstance(item, dict):
                continue
            if "id" in item and "method" in item:
                self._answer_server_request(item)
                continue
            if "id" not in item:
                continue  # server notification
            if item.get("id") != wanted:
                continue  # stale response from an earlier timed-out call
            return item

    def _answer_server_request(self, message: dict) -> None:
        # We advertise no client capabilities, so sampling/roots/elicitation
        # requests cannot be served.  Answering keeps the server unblocked.
        response = {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": "client does not implement this method"},
        }
        try:
            self.send(response)
        except MCPTransportError:
            pass

    # ----------------------------------------------------------------- close

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            self._terminate_tree(proc)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for thread in (self._reader, self._err_reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
        self._inbox.put(_EOF)

    @staticmethod
    def _terminate_tree(proc) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                return
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                proc.terminate()
            except OSError:
                pass
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass

    # --------------------------------------------------------------- helpers

    def _check_open(self) -> None:
        if self._closed:
            raise MCPTransportError(f"MCP server '{self.name}' is closed")

    def _exit_message(self) -> str:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        code = proc.poll() if proc is not None else None
        text = f"MCP server '{self.name}' exited"
        if code is not None:
            text += f" with code {code}"
        tail = self.stderr_tail()
        return text + (f":\n{tail}" if tail else "")

    def _timeout_message(self, timeout) -> str:
        text = f"MCP server '{self.name}' did not answer within {float(timeout):g}s"
        tail = self.stderr_tail()
        return text + (f":\n{tail}" if tail else "")


class HttpTransport:
    """Minimal Streamable HTTP client (one POST per message)."""

    def __init__(self, url, headers=None, name="mcp"):
        self.url = url
        self.headers = dict(headers or {})
        self.name = name
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._closed = False
        self._direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._default = urllib.request.build_opener()

    def start(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise MCPTransportError(f"invalid MCP url: {self.url}")

    def notify(self, message: dict) -> None:
        self._post(message, timeout=10.0, want_response=False)

    def call(self, message: dict, timeout: float) -> dict:
        payload = self._post(message, timeout=float(timeout), want_response=True)
        return payload

    def _post(self, message: dict, timeout: float, want_response: bool):
        self._check_open()
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=data, headers=self._headers(message), method="POST"
        )
        try:
            response = self._opener().open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise MCPTransportError(f"MCP HTTP {exc.code} from {self.name}: {detail}") from None
        except urllib.error.URLError as exc:
            raise MCPTransportError(
                f"cannot reach MCP server '{self.name}': {exc.reason}"
            ) from None
        except (OSError, TimeoutError) as exc:
            raise MCPTransportError(f"MCP HTTP request failed for '{self.name}': {exc}") from None

        with response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                self.session_id = session
            if not want_response:
                return None

            content_type = (
                (response.headers.get("Content-Type") or "").split(";")[0].lower().strip()
            )
            if content_type == "text/event-stream":
                return self._read_sse(response, message.get("id"), timeout)

            raw = response.read(MAX_MESSAGE_BYTES)
            if not raw:
                raise MCPTransportError(f"MCP server '{self.name}' returned an empty response")
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                raise MCPTransportError(
                    f"MCP server '{self.name}' returned invalid JSON: {raw[:300]!r}"
                ) from None
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict) and item.get("id") == message.get("id"):
                        return item
                raise MCPTransportError(f"MCP server '{self.name}' returned no matching response")
            if not isinstance(payload, dict):
                raise MCPTransportError(f"MCP server '{self.name}' returned an invalid message")
            return payload

    def _read_sse(self, response, wanted, timeout: float) -> dict:
        deadline = time.monotonic() + max(0.1, timeout)
        data_lines: list[str] = []
        while True:
            if time.monotonic() > deadline:
                raise MCPTransportError(self._timeout_message(timeout))
            try:
                line = response.readline()
            except (OSError, TimeoutError) as exc:
                raise MCPTransportError(f"MCP SSE read failed for '{self.name}': {exc}") from None
            if not line:
                raise MCPTransportError(
                    f"MCP server '{self.name}' closed the SSE stream without a response"
                )
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if data_lines:
                    payload = _parse_sse_json(data_lines, self.name)
                    data_lines = []
                    if (
                        isinstance(payload, dict)
                        and payload.get("id") == wanted
                        and ("result" in payload or "error" in payload)
                    ):
                        return payload
                continue
            if text.startswith(":"):
                continue
            if text.startswith("data:"):
                data_lines.append(text[5:].lstrip())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.session_id:
            return
        headers = self._headers({"method": "delete"})
        headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(self.url, headers=headers, method="DELETE")
        try:
            self._opener().open(request, timeout=5).close()
        except Exception:
            pass  # optional per spec; servers may not support it

    # --------------------------------------------------------------- helpers

    def _headers(self, message: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version and message.get("method") != "initialize":
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _opener(self):
        host = (urllib.parse.urlsplit(self.url).hostname or "").lower()
        if host == "localhost" or host == "::1" or host.startswith("127."):
            return self._direct
        return self._default

    def _check_open(self) -> None:
        if self._closed:
            raise MCPTransportError(f"MCP server '{self.name}' is closed")

    def _timeout_message(self, timeout) -> str:
        return f"MCP server '{self.name}' did not answer within {float(timeout):g}s"


def _parse_sse_json(data_lines, name):
    raw = "\n".join(data_lines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise MCPTransportError(
            f"MCP server '{name}' sent invalid SSE data: {raw[:300]!r}"
        ) from None


def _http_error_detail(exc) -> str:
    try:
        raw = exc.read(4096)
    except Exception:
        raw = b""
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")[:300]
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:300]
            if payload.get("message"):
                return str(payload["message"])[:300]
        return str(payload)[:300]
    return str(exc.reason or exc)


def _cmd_quote(part: str) -> str:
    if not part:
        return '""'
    if any(ch in part for ch in ' \t"&|<>^()%!'):
        return '"' + part.replace('"', '""') + '"'
    return part


def _decode_stderr(raw: bytes) -> str:
    """cmd.exe writes OEM text while MCP servers normally write UTF-8."""
    encodings = ["utf-8", locale.getpreferredencoding(False), "mbcs"]
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")
