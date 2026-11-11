import unittest
from types import SimpleNamespace

from aider.agent.llm import (
    ContextWindowError,
    LLMClient,
    parse_tool_arguments,
)


def make_model(**overrides):
    values = dict(name="test/model", use_temperature=True, extra_params=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def chunk(delta=None, finish=None, usage=None):
    return {"choices": [{"delta": delta or {}, "finish_reason": finish}], "usage": usage}


class FakeCompletion:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def http_error(status):
    error = RuntimeError(f"HTTP {status}")
    error.status_code = status
    return error


class TestLLMClient(unittest.TestCase):
    def test_stream_accumulates_text_and_fragmented_tool_calls(self):
        chunks = [
            chunk({"content": "Let me "}),
            chunk({"content": "look."}),
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "function": {"name": "read_file", "arguments": '{"pa'},
                        }
                    ]
                }
            ),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"}'}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"name": "run_command"}}]}),
            chunk({"tool_calls": [{"index": 1, "function": {"arguments": "{}"}}]}),
            chunk({}, finish="tool_calls"),
        ]
        seen = []
        client = LLMClient(
            make_model(),
            tools=[{"type": "function"}],
            completion_fn=FakeCompletion(iter(chunks)),
            on_text=seen.append,
        )

        response = client.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(response.content, "Let me look.")
        self.assertEqual(seen, ["Let me ", "look."])
        self.assertEqual([call.name for call in response.tool_calls], ["read_file", "run_command"])
        self.assertEqual(response.tool_calls[0].arguments, '{"path": "a.py"}')
        self.assertEqual(response.tool_calls[0].id, "call_a")
        self.assertEqual(response.tool_calls[1].id, "call_1")
        self.assertEqual(response.finish_reason, "tool_calls")

    def test_non_stream_parses_tool_calls_and_usage(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "x",
                                "function": {"name": "search_text", "arguments": '{"q": "x"}'},
                            }
                        ],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        }
        client = LLMClient(make_model(), stream=False, completion_fn=FakeCompletion(response))

        result = client.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(result.content, "")
        self.assertEqual(result.tool_calls[0].name, "search_text")
        self.assertEqual(result.usage["total_tokens"], 7)

    def test_legacy_function_call_is_accepted(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "function_call": {"name": "read_file", "arguments": '{"path": "a"}'},
                    },
                    "finish_reason": "function_call",
                }
            ]
        }
        client = LLMClient(make_model(), stream=False, completion_fn=FakeCompletion(response))

        result = client.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(result.tool_calls[0].name, "read_file")
        self.assertEqual(result.tool_calls[0].id, "call_0")

    def test_build_kwargs_merges_extra_params_and_temperature(self):
        model = make_model(extra_params={"api_base": "http://local", "extra_headers": {"a": "b"}})
        client = LLMClient(model, tools=[{"type": "function"}], stream=False)

        kwargs = client.build_kwargs([{"role": "user", "content": "hi"}])

        self.assertEqual(kwargs["temperature"], 0)
        self.assertEqual(kwargs["api_base"], "http://local")
        self.assertEqual(kwargs["extra_headers"], {"a": "b"})
        self.assertEqual(kwargs["tool_choice"], "auto")

    def test_no_tools_key_when_no_tools(self):
        client = LLMClient(make_model(), stream=False)
        kwargs = client.build_kwargs([{"role": "user", "content": "hi"}])
        self.assertNotIn("tools", kwargs)

    def test_messages_are_sanitized_for_utf8(self):
        fake = FakeCompletion(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
        client = LLMClient(make_model(), stream=False, completion_fn=fake)
        client.complete([{"role": "user", "content": "bad\ud800char"}])

        sent = fake.calls[0]["messages"][0]["content"]
        self.assertNotIn("\ud800", sent)

    def test_context_window_error_is_raised(self):
        class ContextWindowExceededError(Exception):
            pass

        client = LLMClient(
            make_model(),
            stream=False,
            completion_fn=FakeCompletion(ContextWindowExceededError("too long")),
        )
        with self.assertRaises(ContextWindowError):
            client.complete([{"role": "user", "content": "hi"}], max_retries=0)

    def test_retryable_error_is_retried(self):
        ok = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        fake = FakeCompletion(http_error(500), http_error(429), ok)
        delays = []
        client = LLMClient(make_model(), stream=False, completion_fn=fake, sleep_fn=delays.append)

        result = client.complete([{"role": "user", "content": "hi"}])

        self.assertEqual(result.content, "ok")
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_non_retryable_error_is_not_retried(self):
        fake = FakeCompletion(http_error(400))
        client = LLMClient(make_model(), stream=False, completion_fn=fake)

        with self.assertRaises(RuntimeError):
            client.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(fake.calls), 1)

    def test_parse_tool_arguments(self):
        self.assertEqual(parse_tool_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_tool_arguments(""), {})
        with self.assertRaises(ValueError):
            parse_tool_arguments("{bad")
        with self.assertRaises(ValueError):
            parse_tool_arguments("[1, 2]")


if __name__ == "__main__":
    unittest.main()
