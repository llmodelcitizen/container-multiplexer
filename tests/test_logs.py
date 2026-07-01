from __future__ import annotations

import contextlib
import io
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

    @unittest.expectedFailure
    def test_cmd_logs_replaces_non_utf8_bytes_without_crashing(self) -> None:
        container = FakeContainer("cm-001", logs_output=[b"ok\xff\n"])

        result, output = self.run_logs(container)

        self.assertEqual(result, 0)
        self.assertEqual(output, "ok\ufffd\n")


if __name__ == "__main__":
    unittest.main()
