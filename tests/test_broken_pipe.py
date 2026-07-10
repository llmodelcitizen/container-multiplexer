from __future__ import annotations

import unittest
from unittest import mock

from tests.support import load_cm


class MainBrokenPipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_broken_pipe_test")

    def test_main_returns_141_on_broken_pipe(self) -> None:
        def boom(args):
            raise BrokenPipeError

        original_argv = self.cm.sys.argv
        self.cm.cmd_version = boom
        self.cm.sys.argv = ["cm", "version"]
        try:
            with mock.patch.object(self.cm.os, "dup2") as dup2, \
                    mock.patch.object(self.cm.os, "open", return_value=99):
                result = self.cm.main()
        finally:
            self.cm.sys.argv = original_argv

        self.assertEqual(result, 141)
        dup2.assert_called_once()
