import os
import tempfile
import unittest
from pathlib import Path

from aider.agent.edit_engine import apply_edit_text, resolve_path


def block(path, search, replace):
    return f"{path}\n<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"


def fenced_block(path, search, replace):
    return (
        "```python\n"
        f"{path}\n"
        "<<<<<<< SEARCH\n"
        f"{search}=======\n"
        f"{replace}>>>>>>> REPLACE\n"
        "```\n"
    )


class TestEditEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()

    def write(self, rel, content, newline="\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if newline == "\r\n":
            path.write_bytes(content.replace("\n", "\r\n").encode())
        else:
            path.write_bytes(content.encode())

    def read(self, rel):
        return (self.root / rel).read_bytes().decode()

    def test_exact_replace(self):
        self.write("a.py", "def add(a, b):\n    return a - b\n")

        result = apply_edit_text(
            block("a.py", "    return a - b\n", "    return a + b\n"), self.root
        )

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.files[0].status, "applied")
        self.assertIn("return a + b", self.read("a.py"))
        self.assertEqual((result.files[0].added, result.files[0].removed), (1, 1))

    def test_two_blocks_same_file_sequential(self):
        self.write("a.py", "one\ntwo\n")

        text = block("a.py", "one\n", "ONE\n") + block("a.py", "ONE\n", "FINAL\n")
        result = apply_edit_text(text, self.root)

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.read("a.py"), "FINAL\ntwo\n")
        self.assertEqual(result.files[0].blocks, 2)

    def test_indented_search_matches(self):
        self.write("a.py", "def f():\n    if True:\n        return 1\n")

        result = apply_edit_text(
            block("a.py", "if True:\n    return 1\n", "if True:\n    return 2\n"), self.root
        )

        self.assertTrue(result.ok, result.to_dict())
        self.assertIn("return 2", self.read("a.py"))

    def test_create_new_file(self):
        result = apply_edit_text(
            block("pkg/new.py", "", "def hello():\n    return 'hi'\n"), self.root
        )

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.files[0].status, "created")
        self.assertEqual(self.read("pkg/new.py"), "def hello():\n    return 'hi'\n")

    def test_new_file_with_search_fails(self):
        result = apply_edit_text(block("missing.py", "old\n", "new\n"), self.root)

        self.assertFalse(result.ok)
        self.assertIn("write_file", result.errors[0].error)
        self.assertFalse((self.root / "missing.py").exists())

    def test_missing_search_reports_did_you_mean(self):
        self.write("a.py", "def add(a, b):\n    return a + b\n")

        result = apply_edit_text(
            block("a.py", "    return a + c\n", "    return a * b\n"), self.root
        )

        self.assertFalse(result.ok)
        self.assertEqual(self.read("a.py"), "def add(a, b):\n    return a + b\n")
        self.assertIn("return a + b", result.errors[0].did_you_mean or "")

    def test_multi_file_partial_failure(self):
        self.write("good.py", "value = 1\n")
        self.write("bad.py", "value = 2\n")

        text = block("good.py", "value = 1\n", "value = 10\n") + block(
            "bad.py", "value = 99\n", "value = 20\n"
        )
        result = apply_edit_text(text, self.root)

        self.assertFalse(result.ok)
        self.assertEqual(self.read("good.py"), "value = 10\n")
        self.assertEqual(self.read("bad.py"), "value = 2\n")
        self.assertEqual({f.path: f.status for f in result.files}["good.py"], "applied")
        self.assertEqual({f.path: f.status for f in result.files}["bad.py"], "failed")

    def test_per_file_atomicity(self):
        self.write("a.py", "alpha\nbeta\n")

        text = block("a.py", "alpha\n", "ALPHA\n") + block("a.py", "gamma\n", "GAMMA\n")
        result = apply_edit_text(text, self.root)

        self.assertFalse(result.ok)
        self.assertEqual(self.read("a.py"), "alpha\nbeta\n")
        self.assertEqual(result.errors[0].block_index, 2)

    def test_path_traversal_rejected(self):
        result = apply_edit_text(block("../evil.py", "", "pwned\n"), self.root)

        self.assertFalse(result.ok)
        self.assertIn("escapes the repository", result.errors[0].error)
        self.assertFalse((Path(self.tmp.name) / "evil.py").exists())

    def test_absolute_path_rejected(self):
        target = Path(self.tmp.name) / "abs.py"
        result = apply_edit_text(block(str(target), "", "pwned\n"), self.root)

        self.assertFalse(result.ok)
        self.assertIn("absolute path", result.errors[0].error)
        self.assertFalse(target.exists())

    def test_resolve_path_rejects_drive_relative_on_windows(self):
        if os.name != "nt":
            self.skipTest("windows-only path form")
        with self.assertRaises(ValueError):
            resolve_path(self.root, "D:evil.py")

    def test_crlf_preserved(self):
        self.write("win.py", "line1\nline2\n", newline="\r\n")

        result = apply_edit_text(block("win.py", "line2\n", "LINE2\n"), self.root)

        self.assertTrue(result.ok, result.to_dict())
        data = (self.root / "win.py").read_bytes()
        self.assertEqual(data, b"line1\r\nLINE2\r\n")

    def test_fenced_block_with_filename(self):
        self.write("a.py", "x = 1\n")

        result = apply_edit_text(fenced_block("a.py", "x = 1\n", "x = 2\n"), self.root)

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.read("a.py"), "x = 2\n")

    def test_shell_block_warns(self):
        self.write("a.py", "x = 1\n")
        text = "```bash\npytest -q\n```\n" + block("a.py", "x = 1\n", "x = 3\n")

        result = apply_edit_text(text, self.root)

        self.assertTrue(result.ok, result.to_dict())
        self.assertTrue(any("run_command" in w for w in result.warnings))
        self.assertEqual(self.read("a.py"), "x = 3\n")

    def test_empty_edit_text(self):
        result = apply_edit_text("   \n", self.root)
        self.assertFalse(result.ok)

    def test_malformed_block_reports_parser_error(self):
        result = apply_edit_text("a.py\n<<<<<<< SEARCH\nno divider\n", self.root)

        self.assertFalse(result.ok)
        self.assertTrue(result.errors[0].error)


if __name__ == "__main__":
    unittest.main()
