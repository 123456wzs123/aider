import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from aider.agent.cli import build_parser, parse_args, with_paths
from aider.agent.prompt import build_system_prompt
from aider.agent.repo_context import RepoContext
from aider.agent.ui import AgentUI


class MapModel:
    name = "fake/model"
    info = {"max_input_tokens": 8000, "max_output_tokens": 2048}

    def token_count(self, payload):
        if isinstance(payload, str):
            return max(1, len(payload) // 4)
        return 1

    def get_repo_map_tokens(self):
        return 256


class TestParser(unittest.TestCase):
    def test_defaults(self):
        args = parse_args([])
        self.assertIsNone(args.model)
        self.assertIsNone(args.message)
        self.assertEqual(args.max_steps, 40)
        self.assertFalse(args.yes)
        self.assertFalse(args.continue_session)

    def test_flags(self):
        args = parse_args(["-m", "hi", "--yes", "--max-steps", "5", "--no-stream"])
        self.assertEqual(args.message, "hi")
        self.assertTrue(args.yes)
        self.assertEqual(args.max_steps, 5)
        self.assertTrue(args.no_stream)

    def test_help_mentions_name(self):
        self.assertIn("aider-agent", build_parser().format_help())

    def test_with_paths(self):
        self.assertEqual(with_paths("fix it", []), "fix it")
        self.assertIn("Focus files: a.py, b.py", with_paths("fix it", ["a.py", "b.py"]))


class TestPrompt(unittest.TestCase):
    def test_prompt_contains_edit_contract(self):
        text = build_system_prompt(Path.cwd())

        self.assertIn("edit_file", text)
        self.assertIn("<<<<<<< SEARCH", text)
        self.assertIn(">>>>>>> REPLACE", text)
        self.assertIn("run_command", text)
        self.assertIn(str(Path.cwd().resolve()), text)


class TestUI(unittest.TestCase):
    def render(self, fn):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            fn()
        return buffer.getvalue()

    def test_plain_output(self):
        ui = AgentUI(plain=True)

        header = self.render(
            lambda: ui.header("repo", "main", "model", "search/replace", True, "ask")
        )
        self.assertIn("aider-agent", header)
        self.assertIn("repo @ main", header)

        self.assertIn("> hello", self.render(lambda: ui.user("hello")))

        ui.tool_call("read_file", {"path": "a.py"})
        output = self.render(
            lambda: ui.tool_result(
                "read_file", {"ok": True, "start_line": 1, "end_line": 2, "total_lines": 2}
            )
        )
        self.assertIn("read_file(a.py)", output)
        self.assertIn("1-2 of 2 lines", output)

    def test_plain_error_and_failure(self):
        ui = AgentUI(plain=True)
        ui.tool_call("read_file", {"path": "nope.py"})
        output = self.render(
            lambda: ui.tool_result("read_file", {"ok": False, "error": "file not found"})
        )
        self.assertIn("x file not found", output)

    def test_ascii_glyphs(self):
        ui = AgentUI(plain=True, ascii_only=True)
        self.assertEqual(ui.glyphs["ok"], "+")
        self.assertEqual(ui.glyphs["fail"], "x")
        rich_ui = AgentUI(plain=False, ascii_only=False)
        self.assertEqual(rich_ui.glyphs["ok"], "✓")


class TestRepoContext(unittest.TestCase):
    def test_non_git_fallback_and_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text("def hello():\n    return 1\n")

            context = RepoContext(root, MapModel())
            self.assertIn("sample.py", context.tracked_files())

            text = context.repo_map()
            if text is not None:
                self.assertIn("sample.py", text)
            context.close()

    def test_map_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = RepoContext(Path(tmp), MapModel(), map_tokens=0)
            self.assertIsNone(context.repo_map())

    def test_import_entrypoint(self):
        from aider.agent.cli import main

        self.assertTrue(callable(main))


if __name__ == "__main__":
    unittest.main()
