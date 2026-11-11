"""Tool registry and shared tool context for the agent shell."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ToolContext:
    root: Path
    valid_fnames: list[str] = field(default_factory=list)
    permissions: object | None = None
    ui: object | None = None
    dry_run: bool = False
    repo: object | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], dict]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict]:
        return [spec.schema() for spec in self._tools.values()]

    def dispatch(self, name: str, arguments: dict, context: ToolContext) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return _json({"ok": False, "error": f"unknown tool: {name}"})
        try:
            result = spec.handler(context, arguments or {})
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
            return _json(result)
        except Exception as exc:  # a broken tool must not kill the loop
            return _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def build_registry() -> ToolRegistry:
    """Register the built-in tools (imported lazily to avoid cycles)."""
    from .edit import EDIT_TOOL
    from .files import LIST_TOOL, READ_TOOL, WRITE_TOOL
    from .search import SEARCH_TOOL
    from .shell import RUN_TOOL

    registry = ToolRegistry()
    for spec in (READ_TOOL, LIST_TOOL, SEARCH_TOOL, EDIT_TOOL, WRITE_TOOL, RUN_TOOL):
        registry.register(spec)
    return registry
