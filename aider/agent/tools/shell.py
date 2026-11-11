"""run_command: shell execution with approval, timeout and output caps."""

from __future__ import annotations

import locale
import subprocess

from . import ToolSpec

MAX_OUTPUT_CHARS = 30_000
HEAD_CHARS = 20_000
TAIL_CHARS = 8_000


def _decode(data: bytes) -> str:
    candidates = []
    try:
        candidates.append(locale.getpreferredencoding(False))
    except Exception:
        pass
    candidates.append("utf-8")
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return (
        text[:HEAD_CHARS] + f"\n...[truncated {omitted} chars]...\n" + text[-TAIL_CHARS:],
        True,
    )


def run_command(context, args) -> dict:
    command = str(args.get("command") or "").strip()
    if not command:
        return {"ok": False, "error": "command is required"}

    try:
        timeout = max(1, min(int(args.get("timeout") or 120), 600))
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout must be an integer number of seconds"}

    if context.permissions is not None:
        if not context.permissions.allow_command(command, context.ui):
            return {
                "ok": False,
                "skipped": True,
                "command": command,
                "error": "command was declined; ask the user or try another approach",
            }

    if context.dry_run:
        return {"ok": True, "dry_run": True, "command": command}

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(context.root),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "timeout": True,
            "command": command,
            "error": f"command timed out after {timeout}s",
        }
    except OSError as exc:
        return {"ok": False, "command": command, "error": f"failed to start command: {exc}"}

    output = _decode(proc.stdout)
    stderr = _decode(proc.stderr)
    if stderr:
        output += ("\n" if output else "") + stderr
    output, truncated = _truncate(output)

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": command,
        "output": output,
        "truncated": truncated,
    }


RUN_TOOL = ToolSpec(
    name="run_command",
    description="Run a shell command in the repository root. Returns exit code and output.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
            "timeout": {"type": "integer", "description": "Seconds, default 120, max 600."},
        },
        "required": ["command"],
    },
    handler=run_command,
)
