import json
import tempfile
import unittest
from pathlib import Path

from aider.agent.llm import ContextWindowError, LLMResponse, ToolCall
from aider.agent.runtime import AgentRuntime
from aider.agent.session import SessionStore
from aider.agent.tools import build_registry


class FakeModel:
    name = "fake/model"
    extra_params = None
    use_temperature = True

    def __init__(self, max_input_tokens=128_000, max_output_tokens=8192):
        self.info = {
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
        }

    def token_count(self, payload):
        if isinstance(payload, str):
            return max(1, len(payload) // 4)
        text = json.dumps(payload, ensure_ascii=False)
        return max(1, len(text) // 4)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, max_retries=3):
        self.calls.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRepo:
    def __init__(self):
        self.commits = []

    def commit(self, fnames=None, message=None, aider_edits=False, coder=None):
        self.commits.append({"fnames": list(fnames or []), "message": message})
        return ("abc123", message)


def response(content="", calls=None, usage=None):
    tool_calls = [
        ToolCall(id=f"call_{i}", name=name, arguments=json.dumps(args))
        for i, (name, args) in enumerate(calls or [])
    ]
    return LLMResponse(content=content, tool_calls=tool_calls, usage=usage)


def edit_args(search, replace, path="a.py"):
    return {"edits": f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"}


def assert_tool_pairs(testcase, messages):
    pending = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            pending = {call["id"] for call in message["tool_calls"]}
        elif message.get("role") == "tool":
            testcase.assertIn(message["tool_call_id"], pending)
            pending.discard(message["tool_call_id"])
        elif pending:
            testcase.fail(f"tool_calls without results: {pending}")


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.py").write_text("x = 1\n")
        self.registry = build_registry()

    def make(self, responses, **kwargs):
        client = FakeClient(responses)
        repo = kwargs.pop("repo", None) or FakeRepo()
        runtime = AgentRuntime(
            kwargs.pop("model", FakeModel()),
            self.registry,
            "system prompt",
            root=self.root,
            repo=repo,
            client=client,
            **kwargs,
        )
        return runtime, client, repo

    def test_read_edit_final_and_commit(self):
        runtime, client, repo = self.make(
            [
                response(calls=[("read_file", {"path": "a.py"})]),
                response(calls=[("edit_file", edit_args("x = 1\n", "x = 2\n"))]),
                response(content="done"),
            ]
        )

        result = runtime.run_turn("change x to 2")

        self.assertEqual(result, "done")
        self.assertEqual((self.root / "a.py").read_text(), "x = 2\n")
        self.assertEqual(len(repo.commits), 1)
        self.assertEqual(repo.commits[0]["fnames"], ["a.py"])
        self.assertIn("change x to 2", repo.commits[0]["message"])
        self.assertEqual(len(client.calls), 3)
        assert_tool_pairs(self, runtime.messages())

    def test_failed_edit_is_fed_back(self):
        runtime, _, repo = self.make(
            [
                response(calls=[("edit_file", edit_args("x = 99\n", "x = 3\n"))]),
                response(calls=[("edit_file", edit_args("x = 1\n", "x = 3\n"))]),
                response(content="fixed"),
            ]
        )

        result = runtime.run_turn("fix")

        self.assertEqual(result, "fixed")
        self.assertEqual((self.root / "a.py").read_text(), "x = 3\n")
        messages = runtime.messages()
        tool_contents = [m["content"] for m in messages if m.get("role") == "tool"]
        self.assertIn("SEARCH text not found", tool_contents[0])
        self.assertEqual(len(repo.commits), 1)

    def test_max_steps(self):
        runtime, client, _ = self.make(
            [response(calls=[("read_file", {"path": "a.py"})]) for _ in range(4)],
            max_steps=2,
        )

        result = runtime.run_turn("loop")

        self.assertIn("Stopped after 2 steps", result)
        self.assertTrue(runtime.stopped_at_limit)
        self.assertEqual(len(client.calls), 2)

    def test_invalid_tool_arguments_do_not_crash(self):
        bad = LLMResponse(tool_calls=[ToolCall(id="x", name="read_file", arguments="{bad")])
        runtime, _, _ = self.make([bad, response(content="ok")])

        result = runtime.run_turn("go")

        self.assertEqual(result, "ok")
        contents = [m["content"] for m in runtime.messages() if m.get("role") == "tool"]
        self.assertIn("not valid JSON", contents[0])

    def test_write_file_is_committed(self):
        runtime, _, repo = self.make(
            [
                response(calls=[("write_file", {"path": "new.py", "content": "print(1)\n"})]),
                response(content="ok"),
            ]
        )

        runtime.run_turn("add new file")

        self.assertEqual((self.root / "new.py").read_text(), "print(1)\n")
        self.assertEqual(repo.commits[0]["fnames"], ["new.py"])

    def test_context_trim_keeps_first_task_and_tool_pairs(self):
        runtime, _, _ = self.make([], model=FakeModel(max_input_tokens=4000, max_output_tokens=100))
        runtime.blocks = [
            [{"role": "user", "content": "first task"}],
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_0",
                            "type": "function",
                            "function": {"name": "x", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_0", "content": "y" * 20_000},
            ],
            [{"role": "user", "content": "second task"}],
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "x", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "z" * 20_000},
            ],
            [{"role": "user", "content": "current task"}],
        ]

        runtime._fit_context()

        self.assertEqual(runtime.blocks[0][0]["content"], "first task")
        self.assertLessEqual(len(runtime.blocks), 4)
        assert_tool_pairs(self, runtime.messages())
        for block in runtime.blocks[:-2]:
            for message in block:
                if message.get("role") == "tool":
                    self.assertLessEqual(len(message["content"]), 8100)

    def test_context_error_trims_and_retries(self):
        runtime, client, _ = self.make(
            [ContextWindowError("too big"), response(content="ok after trim")]
        )
        runtime.blocks = [
            [{"role": "user", "content": "old task"}],
            [{"role": "assistant", "content": "old answer"}],
        ]

        result = runtime.run_turn("new task")

        self.assertEqual(result, "ok after trim")
        self.assertEqual(len(runtime.blocks), 3)
        self.assertEqual(runtime.blocks[0][0]["content"], "old task")
        self.assertEqual(runtime.blocks[1][0]["content"], "new task")

    def test_context_error_without_history_raises(self):
        runtime, _, _ = self.make([ContextWindowError("too big")])
        with self.assertRaises(ContextWindowError):
            runtime.run_turn("only task")

    def test_repo_map_is_added_to_first_turn_only(self):
        runtime, client, _ = self.make(
            [response(content="a"), response(content="b")], repo_map="MAP"
        )
        runtime.run_turn("first")
        runtime.run_turn("second")

        self.assertIn("<repo_map>\nMAP", client.calls[0][1]["content"])
        self.assertNotIn("<repo_map>", client.calls[1][-1]["content"])

    def test_usage_is_accumulated(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        runtime, _, _ = self.make(
            [response(content="a", usage=usage), response(content="b", usage=usage)]
        )

        runtime.run_turn("one")
        runtime.run_turn("two")

        self.assertEqual(runtime.usage, {"prompt_tokens": 20, "completion_tokens": 10})

    def test_load_blocks_restores_history(self):
        runtime, _, _ = self.make([])
        blocks = [[{"role": "user", "content": "old"}]]

        self.assertEqual(runtime.load_blocks(blocks), 1)
        self.assertEqual(runtime.messages()[1]["content"], "old")
        self.assertEqual(runtime.load_blocks([{"role": "user"}]), 0)


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_save_and_load_latest(self):
        blocks = [[{"role": "user", "content": "hello"}]]
        first = SessionStore(self.base / "repo", base_dir=self.base).save(blocks, model="m")
        second = SessionStore(self.base / "repo", base_dir=self.base).save(blocks, model="m")
        self.assertNotEqual(first, second)

        loaded = SessionStore(self.base / "repo", base_dir=self.base).load()
        self.assertEqual(loaded["blocks"], blocks)
        self.assertEqual(loaded["model"], "m")
        self.assertEqual(loaded["_path"], str(second))

    def test_save_reuses_path_within_one_session(self):
        store = SessionStore(self.base / "repo", base_dir=self.base)
        blocks = [[{"role": "user", "content": "hello"}]]
        first = store.save(blocks)
        second = store.save(blocks)
        self.assertEqual(first, second)

    def test_invalid_files_are_ignored(self):
        store = SessionStore(self.base / "repo", base_dir=self.base)
        store.directory.mkdir(parents=True)
        (store.directory / "junk.json").write_text("{not json")
        self.assertIsNone(store.load())

        (store.directory / "shape.json").write_text(json.dumps({"blocks": [{"role": "user"}]}))
        self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
