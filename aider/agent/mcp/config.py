"""MCP server configuration: discovery, merging, validation and ``${VAR}`` expansion.

The on-disk format mirrors Claude Desktop so existing configs can be copied:

    {"mcpServers": {"name": {"command": "npx", "args": ["-y", "pkg"], "env": {...}}}}

Files are merged in this order, later entries overriding earlier ones by name:

1. ``~/.aider-agent/mcp.json``            (user level)
2. ``<repo>/.aider-agent/mcp.json``       (project level, needs approval)
3. ``<repo>/.mcp.json``                   (project level, needs approval)
4. every ``--mcp-config PATH``            (explicit, trusted)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 60.0
CONNECT_TIMEOUT = 30.0
MAX_TOOL_TIMEOUT = 3600.0

_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_WIN_VAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


@dataclass
class ServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT
    approval: object = "auto"  # "auto" | "ask" | list of tool names that need approval
    disabled: bool = False
    source: str | None = None
    project: bool = False

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def target(self) -> str:
        if self.url:
            # Never echo query strings: remote MCP urls often carry API keys.
            return self.url.split("#", 1)[0].split("?", 1)[0]
        parts = [self.command or ""] + list(self.args)
        return " ".join(part for part in parts if part)


def config_paths(root, home_dir=None, extra_paths=None) -> list[tuple[Path, bool]]:
    root = Path(root).resolve()
    home = Path(home_dir) if home_dir else Path.home()
    paths = [
        (home / ".aider-agent" / "mcp.json", False),
        (root / ".aider-agent" / "mcp.json", True),
        (root / ".mcp.json", True),
    ]
    for extra in extra_paths or []:
        paths.append((Path(extra).expanduser(), False))
    return paths


def load_server_configs(root, extra_paths=None, home_dir=None, environ=None):
    """Return ``(configs, warnings)`` for every discovered server config."""
    if environ is None:
        environ = os.environ
    environ = dict(environ)

    merged: dict[str, ServerConfig] = {}
    order: list[str] = []
    warnings: list[str] = []

    for path, project in config_paths(root, home_dir=home_dir, extra_paths=extra_paths):
        if not path.is_file():
            continue
        data = _read_json(path, warnings)
        if data is None:
            continue
        servers = _extract_servers(data, path, warnings)
        if servers is None:
            continue
        for raw_name, entry in servers.items():
            name = str(raw_name).strip()
            if not name:
                warnings.append(f"{path}: skipping server with an empty name")
                continue
            cfg, problems = _parse_entry(name, entry, path, project, environ)
            warnings.extend(problems)
            if cfg is None:
                continue
            if name not in merged:
                order.append(name)
            merged[name] = cfg

    return [merged[name] for name in order], warnings


def _read_json(path: Path, warnings: list[str]):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"{path}: cannot read config ({exc})")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"{path}: invalid JSON ({exc})")
        return None


def _extract_servers(data, path: Path, warnings: list[str]):
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        return data["mcpServers"]
    # Convenience: a file that is just {name: {...}} also works.
    if isinstance(data, dict) and data and all(isinstance(v, dict) for v in data.values()):
        return data
    warnings.append(f"{path}: missing an 'mcpServers' object")
    return None


def _parse_entry(name, entry, path, project, environ):
    problems: list[str] = []
    missing_vars: set[str] = set()

    def expand(value):
        return _expand(value, environ, missing_vars)

    if not isinstance(entry, dict):
        return None, [f"{path}: server '{name}' must be an object"]

    disabled = bool(entry.get("disabled", False))
    command = entry.get("command")
    url = entry.get("url")

    if command is not None and not isinstance(command, str):
        return None, [f"{path}: server '{name}': 'command' must be a string"]
    if url is not None and not isinstance(url, str):
        return None, [f"{path}: server '{name}': 'url' must be a string"]
    if command and url:
        return None, [f"{path}: server '{name}': use either 'command' or 'url', not both"]
    if not command and not url:
        if disabled:
            return None, []
        return None, [f"{path}: server '{name}': needs 'command' or 'url'"]

    args = entry.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        return None, [f"{path}: server '{name}': 'args' must be a list of strings"]
    args = [expand(item) for item in args]

    env, bad = _string_dict(entry.get("env"), expand)
    if bad:
        return None, [f"{path}: server '{name}': 'env' must map strings to strings"]
    headers, bad = _string_dict(entry.get("headers"), expand)
    if bad:
        return None, [f"{path}: server '{name}': 'headers' must map strings to strings"]

    timeout = entry.get("timeout", DEFAULT_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        return None, [f"{path}: server '{name}': 'timeout' must be a number"]
    if timeout <= 0:
        return None, [f"{path}: server '{name}': 'timeout' must be positive"]
    timeout = min(timeout, MAX_TOOL_TIMEOUT)

    approval = _parse_approval(entry.get("approval", "auto"))
    if approval is None:
        problems.append(
            f"{path}: server '{name}': 'approval' must be 'auto', 'ask' or a list of tool names"
        )
        approval = "auto"

    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return None, [f"{path}: server '{name}': 'cwd' must be a string"]
    if cwd:
        cwd_path = Path(expand(cwd)).expanduser()
        if not cwd_path.is_absolute():
            cwd_path = (path.parent / cwd_path).resolve()
        cwd = str(cwd_path)

    if command:
        command = expand(command).strip()
        if not command:
            return None, [f"{path}: server '{name}': 'command' is empty"]
    if url:
        url = expand(url).strip()
        if not url.lower().startswith(("http://", "https://")):
            return None, [f"{path}: server '{name}': 'url' must start with http:// or https://"]

    if missing_vars:
        problems.append(
            f"{path}: server '{name}': undefined environment variable(s): "
            + ", ".join(sorted(missing_vars))
        )

    cfg = ServerConfig(
        name=name,
        command=command or None,
        args=args,
        env=env,
        cwd=cwd,
        url=url or None,
        headers=headers,
        timeout=timeout,
        approval=approval,
        disabled=disabled,
        source=str(path),
        project=project,
    )
    return cfg, problems


def _string_dict(value, expand):
    if value is None:
        return {}, False
    if not isinstance(value, dict):
        return {}, True
    result = {}
    for key, item in value.items():
        if not isinstance(item, str):
            return {}, True
        result[str(key)] = expand(item)
    return result, False


def _parse_approval(value):
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("auto", "ask"):
            return lowered
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return None


def _expand(value: str, environ: dict, missing: set[str]) -> str:
    def repl(match):
        key = match.group(1) or match.group(2)
        return _lookup(key, match.group(0), environ, missing)

    def repl_win(match):
        return _lookup(match.group(1), match.group(0), environ, missing)

    value = _VAR_RE.sub(repl, value)
    return _WIN_VAR_RE.sub(repl_win, value)


def _lookup(key: str, original: str, environ: dict, missing: set[str]) -> str:
    if key in environ:
        return environ[key]
    missing.add(key)
    return original
