from __future__ import annotations

import contextlib
import io
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_cm


class TmuxCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_tmux_command_test")

    def test_run_tmux_exits_when_tmux_binary_is_missing(self) -> None:
        with mock.patch.object(self.cm.subprocess, "run", side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.run_tmux(["list-sessions"], check=True)

        self.assertEqual(str(ctx.exception), "Error: tmux is not installed.")

    def test_run_tmux_exits_on_called_process_error(self) -> None:
        error = subprocess.CalledProcessError(1, ["tmux", "bad"])
        with mock.patch.object(self.cm.subprocess, "run", side_effect=error):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.run_tmux(["bad"], check=True)

        self.assertEqual(str(ctx.exception), "Error: tmux command failed: bad")

    def test_exec_tmux_exits_when_tmux_binary_is_missing(self) -> None:
        with mock.patch.object(self.cm.os, "execlp", side_effect=FileNotFoundError):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.exec_tmux(["attach", "-t", "cm"])

        self.assertEqual(str(ctx.exception), "Error: tmux is not installed.")

    def test_get_next_session_name_loops_until_available(self) -> None:
        calls = []
        results = [
            types.SimpleNamespace(returncode=0),
            types.SimpleNamespace(returncode=0),
            types.SimpleNamespace(returncode=1),
        ]

        def fake_run_tmux(args, **kwargs):
            calls.append((args, kwargs))
            return results.pop(0)

        with mock.patch.object(self.cm, "run_tmux", fake_run_tmux):
            name, existed = self.cm.get_next_session_name()

        self.assertEqual((name, existed), ("cm-s3", True))
        self.assertEqual([call[0] for call in calls], [
            ["has-session", "-t", "=cm-s1"],
            ["has-session", "-t", "=cm-s2"],
            ["has-session", "-t", "=cm-s3"],
        ])

    def run_with_stdout(self, func, args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = func(args)
        return result, stdout.getvalue()

    def test_cmd_kill_direct_sessions_and_missing_session_messages(self) -> None:
        calls = []

        def fake_run_tmux(args, **kwargs):
            calls.append(args)
            return types.SimpleNamespace(returncode=0 if args[-1] == "=cm-s1" else 1)

        with mock.patch.object(self.cm, "run_tmux", fake_run_tmux):
            result, output = self.run_with_stdout(
                self.cm.cmd_kill,
                types.SimpleNamespace(sessions=["cm-s1", "missing"]),
            )

        self.assertEqual(result, 0)
        self.assertIn("Killed session 'cm-s1'", output)
        self.assertIn("Session 'missing' not found", output)
        self.assertEqual(calls, [
            ["kill-session", "-t", "=cm-s1"],
            ["kill-session", "-t", "=missing"],
        ])

    def test_cmd_kill_all_filters_cm_sessions_and_prompts(self) -> None:
        calls = []

        def fake_run_tmux(args, **kwargs):
            calls.append(args)
            if args[:2] == ["list-sessions", "-F"]:
                return types.SimpleNamespace(returncode=0, stdout="cm\ncm-s2\nother\n")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "run_tmux", fake_run_tmux), \
                mock.patch("builtins.input", return_value="y"):
            result, output = self.run_with_stdout(
                self.cm.cmd_kill,
                types.SimpleNamespace(sessions=[]),
            )

        self.assertEqual(result, 0)
        self.assertIn("Sessions to kill: cm, cm-s2", output)
        self.assertIn("Killed session 'cm'", output)
        self.assertIn("Killed session 'cm-s2'", output)
        self.assertEqual(calls[1:], [
            ["kill-session", "-t", "=cm"],
            ["kill-session", "-t", "=cm-s2"],
        ])

    def test_cmd_kill_all_aborts_on_declined_prompt(self) -> None:
        def fake_run_tmux(args, **kwargs):
            return types.SimpleNamespace(returncode=0, stdout="cm\n")

        with mock.patch.object(self.cm, "run_tmux", fake_run_tmux), \
                mock.patch("builtins.input", return_value=""):
            result, output = self.run_with_stdout(
                self.cm.cmd_kill,
                types.SimpleNamespace(sessions=[]),
            )

        self.assertEqual(result, 1)
        self.assertIn("Aborted", output)

    def test_cmd_sync_specific_and_all_sessions(self) -> None:
        calls = []

        def fake_specific_run_tmux(args, **kwargs):
            calls.append(args)
            if args[:2] == ["list-windows", "-t"]:
                return types.SimpleNamespace(returncode=0, stdout="0:2\n")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "run_tmux", fake_specific_run_tmux):
            result, output = self.run_with_stdout(
                self.cm.cmd_sync,
                types.SimpleNamespace(state="on", sessions=["cm-s1"]),
            )

        self.assertEqual(result, 0)
        self.assertIn("synchronize-panes on for 'cm-s1'", output)
        self.assertEqual(calls, [
            ["has-session", "-t", "=cm-s1"],
            ["list-windows", "-t", "=cm-s1", "-F", "#{window_index}:#{window_panes}"],
            ["setw", "-t", "=cm-s1:0", "synchronize-panes", "on"],
        ])

        calls.clear()

        def fake_all_run_tmux(args, **kwargs):
            calls.append(args)
            if args[:2] == ["list-sessions", "-F"]:
                return types.SimpleNamespace(returncode=0, stdout="cm\ncm-s2\nnot-cm\n")
            if args[:2] == ["list-windows", "-t"]:
                return types.SimpleNamespace(returncode=0, stdout="0:2\n")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "run_tmux", fake_all_run_tmux):
            result, output = self.run_with_stdout(
                self.cm.cmd_sync,
                types.SimpleNamespace(state="off", sessions=[]),
            )

        self.assertEqual(result, 0)
        self.assertIn("synchronize-panes off for 'cm'", output)
        self.assertIn("synchronize-panes off for 'cm-s2'", output)
        self.assertEqual(calls[1:], [
            ["list-windows", "-t", "=cm", "-F", "#{window_index}:#{window_panes}"],
            ["setw", "-t", "=cm:0", "synchronize-panes", "off"],
            ["list-windows", "-t", "=cm-s2", "-F", "#{window_index}:#{window_panes}"],
            ["setw", "-t", "=cm-s2:0", "synchronize-panes", "off"],
        ])

    def test_cmd_sync_warns_and_skips_window_only_sessions(self) -> None:
        calls = []

        def fake_run_tmux(args, **kwargs):
            calls.append(args)
            if args[:2] == ["list-windows", "-t"]:
                return types.SimpleNamespace(returncode=0, stdout="0:1\n1:1\n")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "run_tmux", fake_run_tmux):
            result, output = self.run_with_stdout(
                self.cm.cmd_sync,
                types.SimpleNamespace(state="on", sessions=["cm-s1"]),
            )

        self.assertEqual(result, 0)
        self.assertIn("cannot synchronize input across windows", output)
        self.assertNotIn(
            ["setw", "-t", "cm-s1", "synchronize-panes", "on"],
            calls,
        )

    def test_cmd_panes_returns_when_no_requested_instances_are_running(self) -> None:
        with mock.patch.object(self.cm, "get_client", return_value=object()), \
                mock.patch.object(self.cm, "get_running_instances", return_value=[]):
            result, output = self.run_with_stdout(
                self.cm.cmd_panes,
                types.SimpleNamespace(instances=["1"], sync=False),
            )

        self.assertEqual(result, 1)
        self.assertIn("No running instances to connect to", output)

    def test_cmd_win_sync_warns_and_switches_existing_tmux_client(self) -> None:
        commands = []
        exec_calls = []

        def fake_run_tmux(args, **kwargs):
            commands.append(args)
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "SCRIPT_DIR", Path("/tmp/cm path")), \
                mock.patch.object(self.cm, "get_client", return_value=object()), \
                mock.patch.object(self.cm, "parse_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_running_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_next_session_name", return_value=("cm-s1", True)), \
                mock.patch.object(self.cm, "run_tmux", fake_run_tmux), \
                mock.patch.object(self.cm, "exec_tmux", lambda args: exec_calls.append(args)), \
                mock.patch.dict(self.cm.os.environ, {"TMUX": "/tmp/tmux"}):
            result, output = self.run_with_stdout(
                self.cm.cmd_win,
                types.SimpleNamespace(instances=["1", "2"], sync=True),
            )

        self.assertIsNone(result)
        self.assertIn("Warning: Existing session found, creating 'cm-s1'", output)
        self.assertIn("cannot synchronize input across windows", output)
        self.assertNotIn(["setw", "-t", "cm-s1", "synchronize-panes", "on"], commands)
        self.assertEqual(exec_calls, [["switch-client", "-t", "=cm-s1"]])

    def test_cmd_panes_kills_partial_session_when_a_split_fails(self) -> None:
        commands = []

        def fake_run_tmux(args, **kwargs):
            commands.append(args)
            if args[0] == "split-window":
                raise SystemExit("Error: tmux command failed: split-window")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "SCRIPT_DIR", Path("/tmp/cm path")), \
                mock.patch.object(self.cm, "get_client", return_value=object()), \
                mock.patch.object(self.cm, "parse_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_running_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_next_session_name", return_value=("cm-s1", False)), \
                mock.patch.object(self.cm, "run_tmux", fake_run_tmux):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.cmd_panes(
                    types.SimpleNamespace(instances=["1", "2"], sync=False)
                )

        self.assertIn("cm-s1", str(ctx.exception))
        self.assertIn(["kill-session", "-t", "=cm-s1"], commands)

    def test_cmd_win_kills_partial_session_when_a_window_fails(self) -> None:
        commands = []

        def fake_run_tmux(args, **kwargs):
            commands.append(args)
            if args[0] == "new-window":
                raise SystemExit("Error: tmux command failed: new-window")
            return types.SimpleNamespace(returncode=0)

        with mock.patch.object(self.cm, "SCRIPT_DIR", Path("/tmp/cm path")), \
                mock.patch.object(self.cm, "get_client", return_value=object()), \
                mock.patch.object(self.cm, "parse_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_running_instances", return_value=[1, 2]), \
                mock.patch.object(self.cm, "get_next_session_name", return_value=("cm-s1", False)), \
                mock.patch.object(self.cm, "run_tmux", fake_run_tmux):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.cmd_win(
                    types.SimpleNamespace(instances=["1", "2"], sync=False)
                )

        self.assertIn("cm-s1", str(ctx.exception))
        self.assertIn(["kill-session", "-t", "=cm-s1"], commands)
