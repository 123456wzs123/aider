"""File tools: read_file, list_files, write_file."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ..edit_engine import resolve_path
from . import ToolSpec

MAX_READ_LINES = 2000
MAX_READ_BYTES = 200_000
MAX_LIST_RESULTS = 500
MAX_WALK_FILES = 20_000

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".aider.tags.cache.v3",
    ".aider.tags.cache.v4",
    "build",
    "dist",
    "target",
}


def _as_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_file(context, args) -> dict:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "error": "path is required"}
    try:
        full = resolve_path(context.root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not full.exists():
        return {"ok": False, "error": f"file not found: {rel}"}
    if full.is_dir():
        return {"ok": False, "error": f"{rel} is a directory; use list_files"}

    try:
        data = full.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"cannot read file: {exc}"}
    if b"\x00" in data[:8192]:
        return {"ok": False, "error": f"{rel} looks like a binary file"}

    text = _as_text(data)
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return {
            "ok": True,
            "path": rel,
            "start_line": 0,
            "end_line": 0,
            "total_lines": 0,
            "content": "",
            "truncated": False,
        }

    try:
        start = int(args.get("start_line") or 1)
        end = args.get("end_line")
        end = int(end) if end is not None else start + MAX_READ_LINES - 1
    except (TypeError, ValueError):
        return {"ok": False, "error": "start_line/end_line must be integers"}

    start = max(1, min(start, total))
    end = max(start, min(end, total))
    content = "\n".join(lines[start - 1 : end])
    truncated = end < total

    if len(content.encode("utf-8")) > MAX_READ_BYTES:
        content = content.encode("utf-8")[:MAX_READ_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    return {
        "ok": True,
        "path": rel,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "content": content,
        "truncated": truncated,
    }


def iter_repo_files(root: Path):
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in IGNORED_DIRS)
        for name in sorted(filenames):
            visited += 1
            if visited > MAX_WALK_FILES:
                return
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix()


def list_files(context, args) -> dict:
    pattern = str(args.get("pattern") or "*").strip() or "*"
    matches = []
    truncated = False

    for rel in iter_repo_files(Path(context.root)):
        basename = rel.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(basename, pattern):
            matches.append(rel)
            if len(matches) >= MAX_LIST_RESULTS:
                truncated = True
                break

    return {"ok": True, "files": matches, "truncated": truncated, "count": len(matches)}


def write_file(context, args) -> dict:
    rel = str(args.get("path") or "").strip()
    if not rel:
        return {"ok": False, "error": "path is required"}
    try:
        full = resolve_path(context.root, rel)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if full.exists() and not args.get("overwrite"):
        return {
            "ok": False,
            "error": f"{rel} already exists; use edit_file, or pass overwrite=true",
        }

    content = args.get("content")
    if content is None:
        return {"ok": False, "error": "content is required"}
    content = str(content).replace("\r\n", "\n").replace("\r", "\n")

    try:
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content.encode("utf-8"))
    except OSError as exc:
        return {"ok": False, "error": f"cannot write file: {exc}"}

    return {
        "ok": True,
        "path": rel,
        "bytes": len(content.encode("utf-8")),
        "lines": len(content.splitlines()),
    }


READ_TOOL = ToolSpec(
    name="read_file",
    description="Read a text file from the repository. Returns 1-based line ranges.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative path."},
            "start_line": {"type": "integer", "description": "First line (1-based)."},
            "end_line": {"type": "integer", "description": "Last line (inclusive)."},
        },
        "required": ["path"],
    },
    handler=read_file,
)

LIST_TOOL = ToolSpec(
    name="list_files",
    description="List repository files matching a glob pattern (e.g. '*.py', 'tests/**').",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, defaults to '*'."}
        },
    },
    handler=list_files,
)

WRITE_TOOL = ToolSpec(
    name="write_file",
    description="Create a new file. Refuses to overwrite unless overwrite=true.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository-relative path."},
            "content": {"type": "string", "description": "Full file content."},
            "overwrite": {"type": "boolean", "description": "Allow replacing an existing file."},
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)
