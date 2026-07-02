from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from tests.support import load_cm


class ArgparseMainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_argparse_main_test")

    def run_main_with_patched_handler(self, argv, handler_name):
        calls = []

        def handler(args):
            calls.append(args)
            return 23

        with mock.patch.object(self.cm, handler_name, handler), \
                mock.patch.object(self.cm.sys, "argv", ["cm.py", *argv]):
            result = self.cm.main()

        self.assertEqual(result, 23)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_main_wires_instance_commands_to_expected_destinations(self) -> None:
        cases = [
            (["start", "1-2"], "cmd_start", {"instances": ["1-2"]}),
            (["stop", "all"], "cmd_stop", {"instances": ["all"]}),
            (["restart", "1", "3"], "cmd_restart", {"instances": ["1", "3"]}),
            (["rm", "2"], "cmd_rm", {"instances": ["2"]}),
            (["logs", "4"], "cmd_logs", {"instance": 4}),
            (["ssh", "-i", "/tmp/key", "5"], "cmd_ssh", {"identity": "/tmp/key", "instance": 5}),
        ]

        for argv, handler_name, expected in cases:
            with self.subTest(argv=argv):
                args = self.run_main_with_patched_handler(argv, handler_name)
                for name, value in expected.items():
                    self.assertEqual(getattr(args, name), value)

    def test_main_wires_update_inspect_tmux_and_simple_commands(self) -> None:
        cases = [
            (
                ["update", "--yes", "--force", "all"],
                "cmd_update",
                {"yes": True, "force": True, "instances": ["all"]},
            ),
            (
                ["inspect", "--no-exec", "--verbose", "--logs", "7", "3"],
                "cmd_inspect",
                {"no_exec": True, "verbose": True, "logs": 7, "instance": 3},
            ),
            (["pan", "--sync", "1", "2"], "cmd_panes", {"sync": True, "instances": ["1", "2"]}),
            (["win", "-s", "1"], "cmd_win", {"sync": True, "instances": ["1"]}),
            (["kill", "cm-s1"], "cmd_kill", {"sessions": ["cm-s1"]}),
            (["sync", "off", "cm-s1"], "cmd_sync", {"state": "off", "sessions": ["cm-s1"]}),
            (["clean"], "cmd_clean", {}),
            (["list"], "cmd_list", {}),
            (["autocomplete"], "cmd_autocomplete", {}),
            (["version"], "cmd_version", {}),
        ]

        for argv, handler_name, expected in cases:
            with self.subTest(argv=argv):
                args = self.run_main_with_patched_handler(argv, handler_name)
                for name, value in expected.items():
                    self.assertEqual(getattr(args, name), value)

    def test_main_help_and_version_action_smoke(self) -> None:
        for argv, expected in ((["--help"], "Manage CM instances"), (["--version"], "cm dev")):
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                with mock.patch.object(self.cm.sys, "argv", ["cm.py", *argv]), \
                        contextlib.redirect_stdout(stdout), \
                        self.assertRaises(SystemExit) as ctx:
                    self.cm.main()

                self.assertEqual(ctx.exception.code, 0)
                self.assertIn(expected, stdout.getvalue())

    def test_main_rejects_invalid_sync_choice(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(self.cm.sys, "argv", ["cm.py", "sync", "maybe"]), \
                contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as ctx:
            self.cm.main()

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_instance_arg_validator_enforces_parse_instances_range(self) -> None:
        self.assertEqual(self.cm._instance_arg("1"), 1)
        self.assertEqual(self.cm._instance_arg("499"), 499)

        with self.assertRaises(self.cm.argparse.ArgumentTypeError) as ctx:
            self.cm._instance_arg("0")
        self.assertIn("must be positive", str(ctx.exception))

        with self.assertRaises(self.cm.argparse.ArgumentTypeError) as ctx:
            self.cm._instance_arg("-1")
        self.assertIn("must be positive", str(ctx.exception))

        with self.assertRaises(self.cm.argparse.ArgumentTypeError) as ctx:
            self.cm._instance_arg("500")
        self.assertIn("exceeds maximum (499)", str(ctx.exception))

        with self.assertRaises(self.cm.argparse.ArgumentTypeError):
            self.cm._instance_arg("abc")

    def test_main_rejects_out_of_range_single_instance_args(self) -> None:
        cases = [
            (["ssh", "9999"], "exceeds maximum (499)"),
            (["ssh", "0"], "must be positive"),
            (["logs", "9999"], "exceeds maximum (499)"),
            (["inspect", "9999"], "exceeds maximum (499)"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with mock.patch.object(self.cm.sys, "argv", ["cm.py", *argv]), \
                        contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as ctx:
                    self.cm.main()

                self.assertEqual(ctx.exception.code, 2)
                err = stderr.getvalue()
                self.assertIn("argument N", err)
                self.assertIn(expected, err)

    def test_main_returns_130_on_keyboard_interrupt(self) -> None:
        def interrupted(args):
            raise KeyboardInterrupt

        stderr = io.StringIO()
        with mock.patch.object(self.cm, "cmd_version", interrupted), \
                mock.patch.object(self.cm.sys, "argv", ["cm.py", "version"]), \
                contextlib.redirect_stderr(stderr):
            result = self.cm.main()

        self.assertEqual(result, 130)
        self.assertIn("Interrupted", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
