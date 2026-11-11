"""Agent loop: request -> tool calls -> observations -> repeat."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .llm import ContextWindowError, LLMClient, parse_tool_arguments
from .tools import ToolContext

MAX_TOOL_RESULT_CHARS = 8000
TRUNCATE_HEAD = 6000
TRUNCATE_TAIL = 2000
DEFAULT_MAX_STEPS = 40


class AgentRuntime:
    def __init__(
        self,
        model,
        registry,
        system_prompt,
        ui=None,
        root=".",
        max_steps=DEFAULT_MAX_STEPS,
        max_input_tokens=None,
        max_output_tokens=None,
        repo=None,
        auto_commits=True,
        dry_run=False,
        client=None,
        tool_context=None,
        repo_map=None,
    ):
        self.model = model
        self.registry = registry
        self.system_prompt = system_prompt
        self.ui = ui
        self.root = Path(root).resolve()
        self.max_steps = max(1, int(max_steps))
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.repo = repo
        self.auto_commits = auto_commits
        self.dry_run = dry_run
        self.repo_map = repo_map
        self.blocks: list[list[dict]] = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.stopped_at_limit = False
        self.llm = client or LLMClient(model, tools=registry.schemas(), on_text=self._on_text)
        self.tool_context = tool_context or ToolContext(root=self.root)
        self.tool_context.root = self.root
        if self.tool_context.ui is None:
            self.tool_context.ui = ui
        self.tool_context.dry_run = dry_run
        self.tool_tokens = self._count_tokens(json.dumps(registry.schemas()))

    # ---------------------------------------------------------------- messages

    def messages(self) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt}]
        for block in self.blocks:
            messages.extend(block)
        return messages

    def load_blocks(self, blocks) -> int:
        if not blocks or not isinstance(blocks[0], list) or not blocks[0]:
            return 0
        if blocks[0][0].get("role") != "user":
            return 0
        self.blocks = [list(block) for block in blocks]
        return len(self.blocks)

    # ------------------------------------------------------------------- turns

    def run_turn(self, user_text: str) -> str:
        self.stopped_at_limit = False
        task = user_text
        if not self.blocks and self.repo_map:
            user_text = f"{user_text}\n\n<repo_map>\n{self.repo_map}\n</repo_map>"
        self.blocks.append([{"role": "user", "content": user_text}])
        changed: set[str] = set()

        for step in range(1, self.max_steps + 1):
            self._fit_context()
            self._ui("step", step, self.max_steps)
            try:
                response = self.llm.complete(self.messages())
            except ContextWindowError as exc:
                if not self._force_trim():
                    raise
                self._ui("warning", f"context window exceeded, dropped old turns: {exc}")
                response = self.llm.complete(self.messages())

            self._track_usage(response)
            if not response.tool_calls:
                self.blocks.append([{"role": "assistant", "content": response.content or ""}])
                self._ui(
                    "status",
                    step,
                    self.max_steps,
                    self._context_tokens(),
                    self._budget(),
                    len(changed),
                )
                return response.content or ""

            block = [self._assistant_message(response)]
            for call in response.tool_calls:
                raw, payload = self._execute_tool(call)
                block.append({"role": "tool", "tool_call_id": call.id, "content": raw})
                self._collect_changes(call.name, payload, changed)
            self.blocks.append(block)

            if changed and self.auto_commits:
                self._auto_commit(changed, task)
                changed.clear()
            self._ui("status", step, self.max_steps, self._context_tokens(), self._budget(), 0)

        self.stopped_at_limit = True
        self._ui("warning", f"stopped after {self.max_steps} steps without a final answer")
        return f"Stopped after {self.max_steps} steps without a final answer."

    def _assistant_message(self, response) -> dict:
        message = {"role": "assistant", "content": response.content or None}
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in response.tool_calls
        ]
        return message

    # ------------------------------------------------------------------- tools

    def _execute_tool(self, call):
        try:
            arguments = parse_tool_arguments(call.arguments)
        except ValueError as exc:
            payload = {"ok": False, "error": str(exc)}
            raw = json.dumps(payload, ensure_ascii=False)
            self._ui("tool_result", call.name, payload, 0.0)
            return raw, payload

        self._ui("tool_call", call.name, arguments)
        start = time.time()
        raw = self.registry.dispatch(call.name, arguments, self.tool_context)
        duration = time.time() - start
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": raw}
        self._ui("tool_result", call.name, payload, duration)
        return raw, payload

    @staticmethod
    def _collect_changes(tool_name, payload, changed: set[str]) -> None:
        if tool_name == "edit_file" and payload.get("ok"):
            for item in payload.get("files", []):
                if item.get("status") in ("applied", "created"):
                    changed.add(item["path"])
        elif tool_name == "write_file" and payload.get("ok"):
            path = payload.get("path")
            if path:
                changed.add(path)

    def _auto_commit(self, paths, task: str) -> None:
        if self.repo is None:
            return
        files = sorted(paths)
        first = (task.strip().splitlines() or ["agent changes"])[0].strip()[:72]
        message = f"{first or 'agent changes'}\n\n" + "\n".join(f"- {path}" for path in files)
        try:
            result = self.repo.commit(fnames=files, message=message, aider_edits=True, coder=None)
            if result:
                self._ui("commit", files)
        except Exception as exc:
            self._ui("warning", f"auto-commit failed: {exc}")

    # ---------------------------------------------------------------- context

    def _count_tokens(self, payload) -> int:
        try:
            return int(self.model.token_count(payload) or 0)
        except Exception:
            return 0

    def _budget(self) -> int:
        info = getattr(self.model, "info", None) or {}
        max_input = self.max_input_tokens or info.get("max_input_tokens") or 128_000
        max_output = self.max_output_tokens or info.get("max_output_tokens") or 8192
        return max(2048, int(max_input) - int(max_output) - 2000)

    def _context_tokens(self) -> int:
        return self._count_tokens(self.messages()) + self.tool_tokens

    def _fit_context(self) -> None:
        self._truncate_old_tool_results()
        while len(self.blocks) > 2 and self._context_tokens() > self._budget():
            self.blocks.pop(1)

    def _truncate_old_tool_results(self) -> None:
        for block in self.blocks[:-2]:
            for message in block:
                if message.get("role") != "tool":
                    continue
                content = message.get("content") or ""
                if len(content) <= MAX_TOOL_RESULT_CHARS:
                    continue
                omitted = len(content) - TRUNCATE_HEAD - TRUNCATE_TAIL
                message["content"] = (
                    content[:TRUNCATE_HEAD]
                    + f"\n...[truncated {omitted} chars]...\n"
                    + content[-TRUNCATE_TAIL:]
                )

    def _force_trim(self) -> bool:
        trimmed = False
        while len(self.blocks) > 2:
            self.blocks.pop(1)
            trimmed = True
        return trimmed

    # ------------------------------------------------------------------- misc

    def _track_usage(self, response) -> None:
        usage = response.usage or {}
        for key in ("prompt_tokens", "completion_tokens"):
            self.usage[key] += int(usage.get(key) or 0)

    def _on_text(self, text: str) -> None:
        self._ui("assistant_delta", text)

    def _ui(self, event: str, *args) -> None:
        if self.ui is None:
            return
        handler = getattr(self.ui, event, None)
        if handler is None:
            return
        try:
            handler(*args)
        except Exception:
            pass
