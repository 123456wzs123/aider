"""Multi-tool LLM client for the agent shell.

aider.models.Model.send_completion() only supports one function schema and
forces tool_choice, so the agent loop needs its own thin client.  Model
settings, extra_params and the fork's UTF-8 sanitizer are reused.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from aider.llm import litellm
from aider.models import sanitize_for_utf8

DEFAULT_TIMEOUT = 600
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class ContextWindowError(Exception):
    """Raised when the provider rejects a request for being too large."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = "{}"


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _usage_dict(usage):
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    return {
        "prompt_tokens": _get(usage, "prompt_tokens"),
        "completion_tokens": _get(usage, "completion_tokens"),
        "total_tokens": _get(usage, "total_tokens"),
    }


def _status_code(exc):
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code


def _is_context_error(exc) -> bool:
    name = type(exc).__name__.lower()
    if "contextwindow" in name or "context_window" in name:
        return True
    message = str(exc).lower()
    return "context window" in message or "maximum context length" in message


def _is_retryable(exc) -> bool:
    if _status_code(exc) in RETRYABLE_STATUS:
        return True
    name = type(exc).__name__.lower()
    markers = ("timeout", "ratelimit", "connection", "unavailable", "internalserver")
    return any(marker in name for marker in markers)


def parse_tool_arguments(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"tool arguments are not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


class _ToolCallAccumulator:
    def __init__(self):
        self._items: dict[int, dict] = {}

    def add(self, deltas) -> None:
        for delta in deltas or []:
            index = _get(delta, "index", 0) or 0
            item = self._items.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = _get(delta, "id")
            if call_id:
                item["id"] = call_id
            function = _get(delta, "function")
            if function is None:
                continue
            name = _get(function, "name")
            if name:
                if not item["name"]:
                    item["name"] = name
                elif name != item["name"]:
                    item["name"] += name
            arguments = _get(function, "arguments")
            if arguments:
                item["arguments"] += arguments

    def build(self) -> list[ToolCall]:
        calls = []
        for index in sorted(self._items):
            item = self._items[index]
            calls.append(
                ToolCall(
                    id=item["id"] or f"call_{index}",
                    name=item["name"],
                    arguments=item["arguments"] or "{}",
                )
            )
        return calls


class LLMClient:
    def __init__(
        self,
        model,
        tools=None,
        temperature=None,
        stream=True,
        timeout=DEFAULT_TIMEOUT,
        on_text=None,
        on_retry=None,
        completion_fn=None,
        sleep_fn=time.sleep,
    ):
        self.model = model
        self.tools = tools or []
        self.temperature = temperature
        self.stream = stream
        self.timeout = timeout
        self.on_text = on_text
        self.on_retry = on_retry
        self._completion_fn = completion_fn
        self._sleep = sleep_fn

    @property
    def completion_fn(self):
        return self._completion_fn or litellm.completion

    def build_kwargs(self, messages, stream=None) -> dict:
        use_stream = self.stream if stream is None else stream
        kwargs = {
            "model": self.model.name,
            "messages": sanitize_for_utf8(messages),
            "stream": use_stream,
        }
        if self.model.use_temperature is not False:
            temperature = self.temperature
            if temperature is None:
                if isinstance(self.model.use_temperature, bool):
                    temperature = 0
                else:
                    temperature = float(self.model.use_temperature)
            kwargs["temperature"] = temperature
        if self.tools:
            kwargs["tools"] = self.tools
            kwargs["tool_choice"] = "auto"
        if self.model.extra_params:
            kwargs.update(self.model.extra_params)
        kwargs.setdefault("timeout", self.timeout)
        return kwargs

    def complete(self, messages, max_retries=3) -> LLMResponse:
        attempt = 0
        while True:
            try:
                return self._complete_once(messages)
            except Exception as exc:
                if _is_context_error(exc):
                    raise ContextWindowError(str(exc)) from exc
                if attempt >= max_retries or not _is_retryable(exc):
                    raise
                delay = min(8.0, 0.5 * (2**attempt))
                if self.on_retry:
                    self.on_retry(attempt + 1, exc, delay)
                self._sleep(delay)
                attempt += 1

    def _complete_once(self, messages) -> LLMResponse:
        kwargs = self.build_kwargs(messages)
        response = self.completion_fn(**kwargs)
        if kwargs["stream"]:
            return self._consume_stream(response)
        return self._consume_response(response)

    def _consume_stream(self, response) -> LLMResponse:
        content_parts: list[str] = []
        accumulator = _ToolCallAccumulator()
        finish_reason = None
        usage = None

        for chunk in response:
            usage = _usage_dict(_get(chunk, "usage")) or usage
            choices = _get(chunk, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = _get(choice, "finish_reason") or finish_reason
            delta = _get(choice, "delta")
            if delta is None:
                continue
            text = _get(delta, "content")
            if text:
                content_parts.append(text)
                if self.on_text:
                    self.on_text(text)
            accumulator.add(_get(delta, "tool_calls"))

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=accumulator.build(),
            finish_reason=finish_reason,
            usage=usage,
        )

    def _consume_response(self, response) -> LLMResponse:
        choices = _get(response, "choices") or []
        if not choices:
            raise RuntimeError("LLM returned no choices")
        choice = choices[0]
        message = _get(choice, "message")
        content = _get(message, "content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

        tool_calls = []
        for index, call in enumerate(_get(message, "tool_calls") or []):
            function = _get(call, "function")
            tool_calls.append(
                ToolCall(
                    id=_get(call, "id") or f"call_{index}",
                    name=_get(function, "name") or "",
                    arguments=_get(function, "arguments") or "{}",
                )
            )

        if not tool_calls:
            legacy = _get(message, "function_call")
            if legacy:
                tool_calls.append(
                    ToolCall(
                        id="call_0",
                        name=_get(legacy, "name") or "",
                        arguments=_get(legacy, "arguments") or "{}",
                    )
                )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=_get(choice, "finish_reason"),
            usage=_usage_dict(_get(response, "usage")),
        )
