from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeClient, load_cm


ROOT = Path(__file__).resolve().parents[1]


class CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_completion_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"

    def run_complete(self, *, mode: str, table: bool, client: FakeClient | None = None):
        stdout = io.StringIO()
        original_get_client = self.cm.get_client
        if client is not None:
            self.cm.get_client = lambda: client
        try:
            with contextlib.redirect_stdout(stdout):
                result = self.cm.cmd_complete(types.SimpleNamespace(mode=mode, table=table))
        finally:
            self.cm.get_client = original_get_client
        return result, stdout.getvalue()

    def test_cmd_complete_instances_scans_workspace_dirs_without_docker(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        for name in ("cm.003", "cm.001", "cm.bad", "notes"):
            (self.cm.WORKSPACES_DIR / name).mkdir()

        result, output = self.run_complete(mode="instances", table=False)

        self.assertEqual(result, 0)
        self.assertEqual(output, "1 3\n")

    def test_cmd_complete_instances_table_uses_docker_summary_statuses(self) -> None:
        client = FakeClient(
            summaries=[
                {"Names": ["/cm-002"], "State": "exited"},
                {"Names": ["/cm-001"], "State": "running"},
                {"Names": ["/bad"], "State": "running"},
            ]
        )

        result, output = self.run_complete(mode="instances", table=True, client=client)

        self.assertEqual(result, 0)
        self.assertIn("#    Container", output)
        self.assertLess(output.index("1    cm-001"), output.index("2    cm-002"))
        self.assertEqual(
            client.api.calls,
            [{"all": True, "filters": {"label": "cm.managed=true"}}],
        )

    def test_cmd_complete_running_mode_plain_and_table(self) -> None:
        client = FakeClient(
            summaries=[
                {"Names": ["/cm-002"], "State": "running"},
                {"Names": ["/cm-001"], "State": "running"},
                {"Names": ["/cm-bad"], "State": "running"},
            ]
        )

        plain_result, plain_output = self.run_complete(mode="running", table=False, client=client)
        table_result, table_output = self.run_complete(mode="running", table=True, client=client)

        self.assertEqual(plain_result, 0)
        self.assertEqual(plain_output, "1 2\n")
        self.assertEqual(table_result, 0)
        self.assertIn("1    cm-001", table_output)
        self.assertIn("2    cm-002", table_output)

    def test_cmd_complete_dead_mode_excludes_running_containers(self) -> None:
        client = FakeClient(
            summaries=[
                {"Names": ["/cm-003"], "State": "exited"},
                {"Names": ["/cm-002"], "State": "running"},
                {"Names": ["/cm-001"], "State": "created"},
            ]
        )

        plain_result, plain_output = self.run_complete(mode="dead", table=False, client=client)
        table_result, table_output = self.run_complete(mode="dead", table=True, client=client)

        self.assertEqual(plain_result, 0)
        self.assertEqual(plain_output, "1 3\n")
        self.assertEqual(table_result, 0)
        self.assertIn("1    cm-001", table_output)
        self.assertIn("3    cm-003", table_output)
        self.assertNotIn("cm-002", table_output)

    def test_main_internal_complete_dispatches_before_argparse(self) -> None:
        calls = []

        def fake_cmd_complete(args):
            calls.append(args)
            return 17

        with mock.patch.object(self.cm, "cmd_complete", fake_cmd_complete), \
                mock.patch.object(self.cm.sys, "argv", ["cm.py", "_complete", "running", "--table"]):
            result = self.cm.main()

        self.assertEqual(result, 17)
        self.assertEqual(calls[0].mode, "running")
        self.assertTrue(calls[0].table)

    def test_autocomplete_script_is_valid_bash(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.cm.cmd_autocomplete(types.SimpleNamespace())

        script = stdout.getvalue()
        with tempfile.NamedTemporaryFile("w", suffix=".bash") as temp:
            temp.write(script)
            temp.flush()
            syntax = subprocess.run(
                ["bash", "-n", temp.name],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result, 0)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn("complete -F _cm_completions cm", script)

    def run_bash_completion_harness(self, harness: str) -> list[str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.cm.cmd_autocomplete(types.SimpleNamespace())
        with tempfile.NamedTemporaryFile("w", suffix=".bash") as temp:
            temp.write(stdout.getvalue())
            temp.flush()
            result = subprocess.run(
                ["bash", "-c", harness, "bash", temp.name],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_autocomplete_keeps_current_word_when_filtering_used_instances(self) -> None:
        completions = self.run_bash_completion_harness(
            r'''
source "$1"
_init_completion() {
    words=(cm stop 1)
    cword=2
    cur=1
    prev=stop
}
cm() {
    if [[ "$1" == "_complete" ]]; then
        printf '1 10\n'
    fi
}
_cm_completions
printf '%s\n' "${COMPREPLY[@]}"
'''
        )

        self.assertEqual(completions, ["1", "10"])

    def test_autocomplete_keeps_cm_session_name_when_filtering_used_sessions(self) -> None:
        completions = self.run_bash_completion_harness(
            r'''
source "$1"
_init_completion() {
    words=(cm kill "")
    cword=2
    cur=""
    prev=kill
}
tmux() {
    printf 'cm\ncm-1\n'
}
_cm_completions
printf '%s\n' "${COMPREPLY[@]}"
'''
        )

        self.assertEqual(completions, ["cm", "cm-1"])

    def test_completion_commands_are_accepted_subcommands(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.cm.cmd_autocomplete(types.SimpleNamespace())
        match = re.search(r'local commands="([^"]+)"', stdout.getvalue())
        self.assertIsNotNone(match)
        completion_commands = match.group(1).split()

        for command in completion_commands:
            with self.subTest(command=command):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "cm.py"), command, "--help"],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_completion_commands_match_public_subcommands(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.cm.cmd_autocomplete(types.SimpleNamespace())
        match = re.search(r'local commands="([^"]+)"', stdout.getvalue())
        self.assertIsNotNone(match)
        completion_commands = set(match.group(1).split())

        help_result = subprocess.run(
            [sys.executable, str(ROOT / "cm.py"), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        parser_match = re.search(r"\{([^}]+)\}", help_result.stdout)
        self.assertIsNotNone(parser_match)
        parser_commands = set(parser_match.group(1).split(","))

        self.assertEqual(completion_commands, parser_commands)


if __name__ == "__main__":
    unittest.main()
