"""search_text: ripgrep when available, pure-Python fallback otherwise."""

from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
from pathlib import Path

from . import ToolSpec
from .files import iter_repo_files

MAX_MATCHES = 100
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 400
RG_TIMEOUT = 30


def _matches_glob(rel: str, pattern: str) -> bool:
    if not pattern or pattern == "*":
        return True
    basename = rel.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(basename, pattern)


def _search_rg(root: Path, pattern: str, glob: str):
    executable = shutil.which("rg")
    if not executable:
        return None
    cmd = [executable, "--line-number", "--no-heading", "--color", "never", "--max-count", "200"]
    if glob and glob != "*":
        cmd += ["--glob", glob]
    cmd += [pattern, "."]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            timeout=RG_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None

    matches = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        path, lineno, text = parts
        if path.startswith("./"):
            path = path[2:]
        try:
            number = int(lineno)
        except ValueError:
            continue
        matches.append({"path": path, "line": number, "text": text[:MAX_LINE_CHARS]})
        if len(matches) >= MAX_MATCHES:
            break
    return {
        "ok": True,
        "matches": matches,
        "count": len(matches),
        "truncated": len(matches) >= MAX_MATCHES,
        "engine": "rg",
    }


def _search_python(root: Path, pattern: re.Pattern, glob: str) -> dict:
    matches = []
    truncated = False
    for rel in iter_repo_files(root):
        if not _matches_glob(rel, glob):
            continue
        full = root / rel
        try:
            if full.stat().st_size > MAX_FILE_BYTES:
                continue
            data = full.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append({"path": rel, "line": number, "text": line[:MAX_LINE_CHARS]})
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    return {
        "ok": True,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
        "engine": "python",
    }


def search_text(context, args) -> dict:
    raw = args.get("pattern", args.get("query"))
    if not raw:
        return {"ok": False, "error": "pattern is required"}
    pattern_text = str(raw)
    glob = str(args.get("glob") or "*").strip() or "*"

    try:
        compiled = re.compile(pattern_text)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}

    root = Path(context.root)
    result = _search_rg(root, pattern_text, glob)
    if result is not None:
        return result
    return _search_python(root, compiled, glob)


SEARCH_TOOL = ToolSpec(
    name="search_text",
    description=(
        "Search repository text with a regular expression. Returns path, line number and text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python/ripgrep regular expression."},
            "glob": {"type": "string", "description": "Optional file glob, e.g. '*.py'."},
        },
        "required": ["pattern"],
    },
    handler=search_text,
)
