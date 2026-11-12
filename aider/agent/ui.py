"""Terminal UI: streaming markdown, tool cards, diffs and confirmations."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

UNICODE_GLYPHS = {
    "prompt": "❯",
    "tool": "⏺",
    "result": "⎿",
    "ok": "✓",
    "fail": "✗",
    "warn": "!",
    "bullet": "·",
}
ASCII_GLYPHS = {
    "prompt": ">",
    "tool": ">",
    "result": "|",
    "ok": "+",
    "fail": "x",
    "warn": "!",
    "bullet": "-",
}


def _stdout_is_utf8() -> bool:
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


def _fmt_tokens(count: int) -> str:
    if count >= 10_000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _json_brief(value, limit=600) -> str:
    try:
        text = json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "…"


class AgentUI:
    def __init__(
        self,
        console=None,
        plain=None,
        verbose=False,
        ascii_only=None,
        history_path=None,
    ):
        if plain is None:
            plain = not sys.stdout.isatty()
        self.plain = plain
        self.verbose = verbose
        no_color = bool(os.environ.get("NO_COLOR"))
        self.console = console or Console(no_color=no_color, highlight=False)
        if ascii_only is None:
            ascii_only = not _stdout_is_utf8()
        self.glyphs = ASCII_GLYPHS if (ascii_only or plain) else UNICODE_GLYPHS
        self.history_path = Path(history_path) if history_path else None
        self._session = None
        self._pending_tool = None
        self._stream_text = ""
        self._md = None
        self._printed_stream = False

    # ---------------------------------------------------------------- chrome

    def header(self, repo, branch, model, edit_format, auto_commit, permissions, mcp=0):
        mcp_note = f" {self.glyphs['bullet']} mcp {mcp}" if mcp else ""
        if self.plain:
            suffix = f" @ {branch}" if branch else ""
            print(f"aider-agent {self.glyphs['bullet']} {repo}{suffix}")
            print(
                f"{model} {self.glyphs['bullet']} {edit_format} {self.glyphs['bullet']} "
                f"auto-commit {'on' if auto_commit else 'off'} {self.glyphs['bullet']} "
                f"{permissions}{mcp_note}"
            )
            return

        body = Text()
        body.append("aider-agent", style="bold cyan")
        body.append(f"  {self.glyphs['bullet']}  {repo}", style="white")
        if branch:
            body.append(f" @ {branch}", style="green")
        body.append("\n")
        body.append(
            f"{model} {self.glyphs['bullet']} {edit_format} {self.glyphs['bullet']} "
            f"auto-commit {'on' if auto_commit else 'off'} {self.glyphs['bullet']} "
            f"{permissions}{mcp_note}",
            style="dim",
        )
        body.append("\n/help for commands · Ctrl-C to cancel", style="dim")
        self.console.print(Panel(body, box=box.ROUNDED, border_style="cyan", padding=(0, 1)))

    def user(self, text: str) -> None:
        if self.plain:
            print(f"{self.glyphs['prompt']} {text}")
        else:
            line = Text()
            line.append(f"{self.glyphs['prompt']} ", style="bold cyan")
            line.append(text, style="bold")
            self.console.print(line)

    def info(self, message: str) -> None:
        if self.plain:
            print(message)
        else:
            self.console.print(Text(message, style="dim"))

    def warning(self, message: str) -> None:
        if self.plain:
            print(f"{self.glyphs['warn']} {message}")
        else:
            self.console.print(Text(f"{self.glyphs['warn']} {message}", style="yellow"))

    def error(self, message: str, detail: str | None = None) -> None:
        if self.plain:
            print(f"{self.glyphs['fail']} {message}")
        else:
            self.console.print(Text(f"{self.glyphs['fail']} {message}", style="bold red"))
        if detail and self.verbose:
            if self.plain:
                print(detail)
            else:
                self.console.print(Text(detail, style="dim red"))

    # ------------------------------------------------------------- assistant

    def assistant_delta(self, text: str) -> None:
        if not text:
            return
        if self.plain:
            print(text, end="", flush=True)
            self._printed_stream = True
            return
        self._stream_text += text
        if self._md is None:
            from aider.mdstream import MarkdownStream

            self._md = MarkdownStream()
        self._md.update(self._stream_text)

    def assistant_end(self) -> None:
        if self.plain:
            if self._printed_stream:
                print()
                self._printed_stream = False
            return
        if self._md is not None:
            self._md.update(self._stream_text, final=True)
            self._md = None
        self._stream_text = ""

    # ----------------------------------------------------------------- tools

    def tool_call(self, name: str, arguments: dict) -> None:
        self._pending_tool = (name, arguments or {}, time.time())

    def tool_result(self, name: str, payload: dict, duration: float = 0.0) -> None:
        if self._pending_tool and self._pending_tool[0] == name:
            _, arguments, started = self._pending_tool
            duration = duration or (time.time() - started)
        else:
            arguments = {}
        self._pending_tool = None

        ok = bool(payload.get("ok"))
        display = self.pretty_name(name)
        args_summary = self._args_summary(name, arguments)
        summary = self._result_summary(name, payload)

        if self.plain:
            print(f"{self.glyphs['tool']} {display}({args_summary}) {duration:.1f}s")
            prefix = self.glyphs["ok"] if ok else self.glyphs["fail"]
            print(f"{self.glyphs['result']} {prefix} {summary}")
            return

        call_line = Text()
        call_line.append(f"{self.glyphs['tool']} ", style="cyan")
        call_line.append(display, style="bold cyan")
        call_line.append(f"({args_summary})", style="dim")
        call_line.append(f"  {duration:.1f}s", style="dim")
        self.console.print(call_line)

        result_line = Text()
        result_line.append(f"  {self.glyphs['result']} ", style="dim")
        result_line.append(
            f"{self.glyphs['ok'] if ok else self.glyphs['fail']} {summary}",
            style="green" if ok else "red",
        )
        self.console.print(result_line)

        if name == "edit_file" and ok:
            self._print_diffs(payload)

    def status(self, step, max_steps, tokens, budget, changed) -> None:
        if self.plain:
            return
        line = (
            f"  step {step}/{max_steps} {self.glyphs['bullet']} "
            f"{_fmt_tokens(tokens)}/{_fmt_tokens(budget)} ctx"
        )
        self.console.print(Text(line, style="dim"))

    def commit(self, files) -> None:
        names = ", ".join(files[:3]) + (" …" if len(files) > 3 else "")
        self.info(f"{self.glyphs['ok']} committed {len(files)} file(s): {names}")

    # ------------------------------------------------------------ confirmation

    def confirm_tool(self, server: str, tool: str, arguments: dict) -> str:
        args_text = _json_brief(arguments)
        if self.plain:
            try:
                bullet = self.glyphs["bullet"]
                return input(f"run MCP {server} {bullet} {tool} {args_text} [y/n/a] ")
            except (EOFError, KeyboardInterrupt):
                return "n"

        body = Text()
        body.append(f"{server} · {tool}", style="bold")
        body.append(f"\n{args_text}", style="dim")
        self.console.print(
            Panel(
                body,
                title="MCP tool",
                box=box.ROUNDED,
                border_style="yellow",
                padding=(0, 1),
            )
        )
        try:
            return input("[y] run  [n] skip  [a] always this tool: ")
        except (EOFError, KeyboardInterrupt):
            return "n"

    def confirm_mcp_server(self, config) -> bool:
        target = config.target()
        if self.plain:
            try:
                answer = input(f"start project MCP server '{config.name}'? {target} [y/n] ")
            except (EOFError, KeyboardInterrupt):
                return False
            return answer.strip().lower().startswith("y")

        body = Text()
        body.append(config.name, style="bold")
        body.append(f"\n{target}", style="dim")
        self.console.print(
            Panel(
                body,
                title="project MCP server",
                box=box.ROUNDED,
                border_style="yellow",
                padding=(0, 1),
            )
        )
        try:
            answer = input("[y] start  [n] skip: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower().startswith("y")

    def confirm_command(self, command: str) -> str:
        if self.plain:
            try:
                return input(f"run? {command} [y/n/a] ")
            except (EOFError, KeyboardInterrupt):
                return "n"

        body = Text(command, style="bold")
        self.console.print(
            Panel(
                body,
                title="run command",
                box=box.ROUNDED,
                border_style="yellow",
                padding=(0, 1),
            )
        )
        try:
            return input("[y] run  [n] skip  [a] always this prefix: ")
        except (EOFError, KeyboardInterrupt):
            return "n"

    # ------------------------------------------------------------------ input

    def read_input(self) -> str:
        prompt = f"{self.glyphs['prompt']} "
        if self.plain:
            return input(prompt)
        try:
            if self._session is None:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.history import FileHistory

                history = None
                if self.history_path:
                    self.history_path.parent.mkdir(parents=True, exist_ok=True)
                    history = FileHistory(str(self.history_path))
                self._session = PromptSession(history=history)
            return self._session.prompt(prompt)
        except Exception:
            return input(prompt)

    # ----------------------------------------------------------------- helpers

    def pretty_name(self, name: str) -> str:
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                return f"{parts[1]} {self.glyphs['bullet']} {parts[2]}"
        return name

    @staticmethod
    def _args_summary(name: str, arguments: dict) -> str:
        if name.startswith("mcp__"):
            return _json_brief(arguments, limit=100)
        if name == "read_file":
            path = arguments.get("path", "")
            start = arguments.get("start_line")
            end = arguments.get("end_line")
            if start or end:
                return f"{path}:{start or 1}-{end or '…'}"
            return str(path)
        if name == "search_text":
            pattern = repr(arguments.get("pattern", ""))
            glob = arguments.get("glob")
            return f"{pattern} in {glob}" if glob else pattern
        if name == "list_files":
            return str(arguments.get("pattern", "*"))
        if name == "run_command":
            return str(arguments.get("command", ""))[:100]
        if name == "edit_file":
            edits = str(arguments.get("edits", ""))
            blocks = edits.count("<<<<<<< SEARCH")
            return f"{blocks} block(s)" if blocks else "edit"
        if name == "write_file":
            return str(arguments.get("path", ""))
        return ""

    @staticmethod
    def _result_summary(name: str, payload: dict) -> str:
        error = payload.get("error")
        if name.startswith("mcp__"):
            if payload.get("dry_run"):
                return "dry-run"
            if payload.get("skipped"):
                return "declined by user"
            if not payload.get("ok"):
                return str(error or "failed").splitlines()[0][:120]
            text = str(payload.get("content") or "").strip()
            return text.splitlines()[0][:100] if text else "ok"
        if name == "run_command":
            if payload.get("skipped"):
                return "declined by user"
            if payload.get("timeout"):
                return "timed out"
            if payload.get("dry_run"):
                return "dry-run"
            output = (payload.get("output") or "").strip()
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            head = lines[-1][:100] if lines else ""
            code = payload.get("exit_code")
            if code is None:
                return str(error or "failed").splitlines()[0][:120]
            return f"exit {code}" + (f" · {head}" if head else "")

        if not payload.get("ok"):
            if payload.get("skipped"):
                return "declined by user"
            if payload.get("timeout"):
                return "timed out"
            if name == "edit_file" and error:
                return str(error).splitlines()[0][:120]
            return str(error or "failed").splitlines()[0][:120]

        if name == "read_file":
            text = (
                f"{payload.get('start_line')}-{payload.get('end_line')} "
                f"of {payload.get('total_lines')} lines"
            )
            return text + (" (truncated)" if payload.get("truncated") else "")
        if name == "list_files":
            return f"{payload.get('count', 0)} file(s)"
        if name == "search_text":
            return f"{payload.get('count', 0)} match(es) via {payload.get('engine')}"
        if name == "edit_file":
            files = payload.get("files", [])
            added = sum(f.get("added", 0) for f in files)
            removed = sum(f.get("removed", 0) for f in files)
            statuses = ", ".join(f"{f['path']} {f['status']}" for f in files[:3])
            return f"{statuses}  +{added} -{removed}"
        if name == "write_file":
            return f"{payload.get('bytes', 0)} bytes written"
        if name == "run_command":
            if payload.get("dry_run"):
                return "dry-run"
            output = (payload.get("output") or "").strip().splitlines()
            head = output[0][:100] if output else ""
            return f"exit {payload.get('exit_code')}" + (f" · {head}" if head else "")
        return "ok"

    def _print_diffs(self, payload: dict) -> None:
        budget = 40
        shown = 0
        more = 0
        for item in payload.get("files", []):
            diff = item.get("diff") or ""
            if not diff:
                continue
            for line in diff.splitlines():
                if shown >= budget:
                    more += 1
                    continue
                style = "dim"
                if line.startswith("+") and not line.startswith("+++"):
                    style = "green"
                elif line.startswith("-") and not line.startswith("---"):
                    style = "red"
                elif line.startswith("@@"):
                    style = "cyan"
                self.console.print(Text("    " + line, style=style))
                shown += 1
        if more:
            self.console.print(Text(f"    … +{more} line(s)", style="dim"))
