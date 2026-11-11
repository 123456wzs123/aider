import os
import shutil
import tempfile
import unittest

from aider.coders import Coder
from aider.commands import Commands
from aider.io import InputOutput
from aider.models import Model


class TestChatLanguage(unittest.TestCase):
    def setUp(self):
        self.original_cwd = os.getcwd()
        self.tempdir = tempfile.mkdtemp()
        os.chdir(self.tempdir)
        self.io = InputOutput(yes=True)
        self.coder = Coder.create(Model("gpt-3.5-turbo"), "whole", io=self.io)

    def tearDown(self):
        os.chdir(self.original_cwd)
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_default_is_none(self):
        self.assertIsNone(self.coder.chat_language)

    def test_normalize_auto(self):
        self.assertEqual(self.coder.normalize_language("Auto"), "auto")

    def test_normalize_windows_locale(self):
        self.assertEqual(self.coder.normalize_language("German_Germany"), "German")

    def test_normalize_chinese_locale_unchanged(self):
        self.assertEqual(self.coder.normalize_language("zh_CN"), "Chinese")

    def test_prompt_explicit_language(self):
        self.coder.chat_language = "Chinese"
        prompt = self.coder.fmt_system_prompt(self.coder.gpt_prompts.main_system)
        self.assertIn("Reply in Chinese", prompt)

    def test_prompt_auto_language(self):
        self.coder.chat_language = "auto"
        prompt = self.coder.fmt_system_prompt(self.coder.gpt_prompts.main_system)
        self.assertIn("same language the user is using", prompt)

    def test_prompt_default_follows_user(self):
        prompt = self.coder.fmt_system_prompt(self.coder.gpt_prompts.main_system)
        # no strict directive: the model follows the user language by default
        self.assertNotIn("Reply in ", prompt)

    def test_cmd_chat_language_set(self):
        cmds = Commands(self.io, self.coder)
        cmds.cmd_chat_language("Chinese")
        self.assertEqual(self.coder.chat_language, "Chinese")

    def test_cmd_chat_language_auto(self):
        cmds = Commands(self.io, self.coder)
        cmds.cmd_chat_language("auto")
        self.assertEqual(self.coder.chat_language, "auto")
