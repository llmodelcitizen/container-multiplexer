from __future__ import annotations

import contextlib
import io
import tempfile
import types
import unittest
from unittest import mock

from tests.support import FakeClient, FakeContainer, load_cm


class LogCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_logs_test")

    def run_logs(self, container: FakeContainer):
        stdout = io.StringIO()
        client = FakeClient({"cm-001": container})
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: client
        try:
            with contextlib.redirect_stdout(stdout):
                result = self.cm.cmd_logs(types.SimpleNamespace(instance=1))
        finally:
            self.cm.get_client = original_get_client
        return result, stdout.getvalue()

    def test_cmd_logs_streams_utf8_lines(self) -> None:
        container = FakeContainer("cm-001", logs_output=[b"one\n", b"two\n"])

        result, output = self.run_logs(container)

        self.assertEqual(result, 0)
        self.assertEqual(output, "one\ntwo\n")
        self.assertEqual(container.log_calls, [{"stream": True, "follow": True}])

    def test_cmd_logs_handles_keyboard_interrupt_during_follow(self) -> None:
        def interrupted_stream(**kwargs):
            yield b"before\n"
            raise KeyboardInterrupt

        container = FakeContainer("cm-001", logs_output=interrupted_stream)

        result, output = self.run_logs(container)

        self.assertEqual(result, 0)
        self.assertEqual(output, "before\n\n")

    def test_cmd_logs_handles_broken_pipe_during_follow(self) -> None:
        def broken_stream(**kwargs):
            yield b"before\n"
            raise BrokenPipeError

        container = FakeContainer("cm-001", logs_output=broken_stream)
        client = FakeClient({"cm-001": container})
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: client
        try:
            with tempfile.TemporaryFile("w+") as tmp, \
                    contextlib.redirect_stdout(tmp), \
                    mock.patch.object(self.cm.os, "dup2") as dup2, \
                    mock.patch.object(self.cm.os, "open", return_value=99):
                result = self.cm.cmd_logs(types.SimpleNamespace(instance=1))
        finally:
            self.cm.get_client = original_get_client

        self.assertEqual(result, 141)
        dup2.assert_called_once()

    def test_cmd_logs_reports_missing_container(self) -> None:
        stdout = io.StringIO()
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: FakeClient()
        try:
            with contextlib.redirect_stdout(stdout):
                result = self.cm.cmd_logs(types.SimpleNamespace(instance=1))
        finally:
            self.cm.get_client = original_get_client

        self.assertEqual(result, 1)
        self.assertIn("Instance 1 does not exist", stdout.getvalue())

    def test_cmd_logs_replaces_non_utf8_bytes_without_crashing(self) -> None:
        container = FakeContainer("cm-001", logs_output=[b"ok\xff\n"])

        result, output = self.run_logs(container)

        self.assertEqual(result, 0)
        self.assertEqual(output, "ok\ufffd\n")

    def test_cmd_logs_decodes_multibyte_characters_split_across_chunks(self) -> None:
        container = FakeContainer("cm-001", logs_output=[b"emoji: \xf0\x9f", b"\x98\x80\n"])

        result, output = self.run_logs(container)

        self.assertEqual(result, 0)
        self.assertEqual(output, "emoji: \U0001f600\n")


if __name__ == "__main__":
    unittest.main()
