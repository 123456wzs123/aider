import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aider.agent.permissions import PermissionPolicy
from aider.agent.tools import ToolContext, ToolRegistry, ToolSpec, build_registry
from aider.agent.tools.files import list_files, read_file, write_file
from aider.agent.tools.search import search_text
from aider.agent.tools.shell import run_command


def python_cmd(code):
    return f'"{sys.executable}" -c "{code}"'


class TestFileTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = ToolContext(root=self.root)

    def write(self, rel, content):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    def test_read_file_full_and_range(self):
        self.write("a.py", "one\ntwo\nthree\n")

        full = read_file(self.context, {"path": "a.py"})
        self.assertTrue(full["ok"])
        self.assertEqual(full["content"], "one\ntwo\nthree")
        self.assertEqual(full["total_lines"], 3)

        part = read_file(self.context, {"path": "a.py", "start_line": 2, "end_line": 2})
        self.assertEqual(part["content"], "two")
        self.assertEqual((part["start_line"], part["end_line"]), (2, 2))

    def test_read_file_errors(self):
        self.write("bin.dat", b"\x00\x01\x02")
        self.assertFalse(read_file(self.context, {"path": "missing.py"})["ok"])
        self.assertFalse(read_file(self.context, {"path": "../x.py"})["ok"])
        self.assertFalse(read_file(self.context, {"path": "bin.dat"})["ok"])
        self.assertFalse(read_file(self.context, {"path": "."})["ok"])

    def test_read_file_truncates_at_line_cap(self):
        self.write("long.txt", "\n".join(f"line{i}" for i in range(2500)))

        result = read_file(self.context, {"path": "long.txt"})

        self.assertEqual(result["end_line"], 2000)
        self.assertEqual(result["total_lines"], 2500)
        self.assertTrue(result["truncated"])

    def test_list_files_glob_and_ignored_dirs(self):
        self.write("a.py", "x")
        self.write("sub/b.py", "x")
        self.write("sub/c.txt", "x")
        self.write(".git/config", "x")
        self.write("node_modules/d.js", "x")

        py = list_files(self.context, {"pattern": "*.py"})
        self.assertEqual(py["files"], ["a.py", "sub/b.py"])

        all_files = list_files(self.context, {})
        self.assertIn("sub/c.txt", all_files["files"])
        self.assertNotIn(".git/config", all_files["files"])
        self.assertNotIn("node_modules/d.js", all_files["files"])

    def test_write_file_create_refuse_and_overwrite(self):
        created = write_file(self.context, {"path": "pkg/a.py", "content": "x = 1\n"})
        self.assertTrue(created["ok"])
        self.assertEqual((self.root / "pkg/a.py").read_text(), "x = 1\n")

        refused = write_file(self.context, {"path": "pkg/a.py", "content": "y = 2\n"})
        self.assertFalse(refused["ok"])
        self.assertIn("edit_file", refused["error"])

        replaced = write_file(
            self.context, {"path": "pkg/a.py", "content": "y = 2\n", "overwrite": True}
        )
        self.assertTrue(replaced["ok"])
        self.assertEqual((self.root / "pkg/a.py").read_text(), "y = 2\n")

    def test_write_file_rejects_traversal(self):
        result = write_file(self.context, {"path": "../evil.py", "content": "x"})
        self.assertFalse(result["ok"])
        self.assertFalse((self.root.parent / "evil.py").exists())


class TestSearchTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = ToolContext(root=self.root)
        (self.root / "a.py").write_text("needle here\n")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.py").write_text("nothing\n")
        (self.root / "c.txt").write_text("needle too\n")

    def test_search_python_fallback(self):
        with mock.patch("aider.agent.tools.search.shutil.which", return_value=None):
            result = search_text(self.context, {"pattern": "needle"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["engine"], "python")
        self.assertEqual(result["count"], 2)

    def test_search_glob_filter(self):
        with mock.patch("aider.agent.tools.search.shutil.which", return_value=None):
            result = search_text(self.context, {"pattern": "needle", "glob": "*.txt"})

        self.assertEqual([m["path"] for m in result["matches"]], ["c.txt"])

    def test_search_invalid_regex(self):
        result = search_text(self.context, {"pattern": "("})
        self.assertFalse(result["ok"])
        self.assertIn("invalid regex", result["error"])

    def test_search_missing_pattern(self):
        self.assertFalse(search_text(self.context, {})["ok"])


class TestShellTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.context = ToolContext(root=self.root)

    def test_denied_command_is_not_executed(self):
        self.context.permissions = PermissionPolicy(approver=lambda cmd: "n")

        result = run_command(self.context, {"command": "echo hi > created.txt"})

        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertFalse((self.root / "created.txt").exists())

    def test_approved_command_executes(self):
        self.context.permissions = PermissionPolicy(approver=lambda cmd: "y")

        result = run_command(self.context, {"command": "echo hi > created.txt"})

        self.assertEqual(result["exit_code"], 0, result)
        self.assertIn("hi", (self.root / "created.txt").read_text())

    def test_nonzero_exit(self):
        result = run_command(self.context, {"command": python_cmd("import sys; sys.exit(3)")})
        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 3)

    def test_timeout(self):
        result = run_command(
            self.context,
            {"command": python_cmd("import time; time.sleep(5)"), "timeout": 1},
        )
        self.assertTrue(result["timeout"])

    def test_dry_run_does_not_execute(self):
        self.context.dry_run = True
        result = run_command(self.context, {"command": "echo hi > created.txt"})
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.root / "created.txt").exists())

    def test_output_is_truncated(self):
        script = self.root / "big.py"
        script.write_text("print('x' * 40000)\n")
        result = run_command(self.context, {"command": f'"{sys.executable}" "{script}"'})
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["output"]), 31000)


class TestPermissions(unittest.TestCase):
    def test_yes_bypasses_approval(self):
        policy = PermissionPolicy(yes=True, approver=lambda cmd: "n")
        self.assertTrue(policy.allow_command("rm -rf /"))

    def test_always_remembers_prefix(self):
        calls = []

        def approver(command):
            calls.append(command)
            return "a"

        policy = PermissionPolicy(approver=approver)
        self.assertTrue(policy.allow_command("pytest -q tests"))
        self.assertTrue(policy.allow_command("pytest -q tests/basic"))
        self.assertEqual(len(calls), 1)

    def test_default_denied_without_ui(self):
        self.assertFalse(PermissionPolicy().allow_command("echo hi"))

    def test_ui_prompt_is_used(self):
        class UI:
            def confirm_command(self, command):
                return "y"

        self.assertTrue(PermissionPolicy().allow_command("echo hi", UI()))


class TestRegistry(unittest.TestCase):
    def test_builtin_registry_has_six_tools(self):
        registry = build_registry()
        self.assertEqual(
            sorted(registry.names()),
            ["edit_file", "list_files", "read_file", "run_command", "search_text", "write_file"],
        )
        self.assertEqual(len(registry.schemas()), 6)

    def test_unknown_tool_and_broken_tool(self):
        registry = ToolRegistry()
        unknown = json.loads(registry.dispatch("nope", {}, ToolContext(root=Path("."))))
        self.assertFalse(unknown["ok"])

        def boom(context, args):
            raise RuntimeError("kaboom")

        registry.register(ToolSpec("boom", "boom", {}, boom))
        result = json.loads(registry.dispatch("boom", {}, ToolContext(root=Path("."))))
        self.assertFalse(result["ok"])
        self.assertIn("kaboom", result["error"])

    def test_edit_tool_through_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("x = 1\n")
            registry = build_registry()
            edits = "a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"

            raw = registry.dispatch("edit_file", {"edits": edits}, ToolContext(root=root))
            result = json.loads(raw)

            self.assertTrue(result["ok"], result)
            self.assertEqual((root / "a.py").read_text(), "x = 2\n")


if __name__ == "__main__":
    unittest.main()
