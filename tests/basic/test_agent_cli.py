import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aider.agent.cli import build_parser, default_config_files, parse_args, with_paths
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

    def test_model_from_config_file(self):
        import yaml

        config = {
            "model": "openai/deepseek-v4.1-flash",
            "max-steps": 7,
            # Classic aider keys the agent does not define must not break it.
            "max-chat-history-tokens": 1024,
        }
        tmp = tempfile.mktemp(suffix=".yml")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f)

            args = parse_args([], [tmp])
            self.assertEqual(args.model, "openai/deepseek-v4.1-flash")
            self.assertEqual(args.max_steps, 7)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def test_model_from_env(self):
        with patch.dict(os.environ, {"AIDER_MODEL": "openai/deepseek-v4.1-flash"}):
            args = parse_args([])
        self.assertEqual(args.model, "openai/deepseek-v4.1-flash")

    def test_default_config_locations(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            home_cfg = Path(home) / ".aider-agent" / "config.yml"
            home_cfg.parent.mkdir()
            home_cfg.write_text("model: home/model\n", encoding="utf-8")

            repo_cfg = Path(root) / ".aider-agent" / "config.yml"
            repo_cfg.parent.mkdir()
            repo_cfg.write_text("model: repo/model\n", encoding="utf-8")

            files = default_config_files(root, home_dir=home)
            self.assertEqual(len(files), 2)
            # Project config is read later, so it wins.
            self.assertEqual(parse_args([], files).model, "repo/model")

    def test_flag_beats_config_and_env(self):
        with patch.dict(os.environ, {"AIDER_MODEL": "env/model"}):
            args = parse_args(["--model", "flag/model"])
        self.assertEqual(args.model, "flag/model")

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

    def test_rich_render_has_color_and_diff(self):
        from rich.console import Console

        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=100, color_system="standard")
        ui = AgentUI(console=console, plain=False, ascii_only=False)

        ui.header("repo", "main", "model", "search/replace", True, "ask")
        ui.tool_call("edit_file", {"edits": "a.py\n<<<<<<< SEARCH\nx\n"})
        ui.tool_result(
            "edit_file",
            {
                "ok": True,
                "files": [
                    {
                        "path": "a.py",
                        "status": "applied",
                        "added": 1,
                        "removed": 1,
                        "diff": "@@ -1 +1 @@\n-x = 1\n+x = 2",
                    }
                ],
            },
            duration=0.2,
        )

        output = buffer.getvalue()
        self.assertIn("aider-agent", output)
        self.assertIn("\x1b[", output)
        self.assertIn("a.py applied", output)
        self.assertIn("x = 2", output)
        self.assertIn("0.2s", output)


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
