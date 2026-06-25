from __future__ import annotations

import importlib.machinery
import importlib.util
import shlex
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


def load_cm_module():
    cm_path = Path(__file__).resolve().parents[1] / "cm.py"
    loader = importlib.machinery.SourceFileLoader("cm_script", str(cm_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TmuxCommandQuotingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm_module()
        self.commands: list[list[str]] = []

        replacements = {
            "SCRIPT_DIR": Path("/tmp/cm path"),
            "get_client": lambda: object(),
            "parse_instances": lambda values: [int(value) for value in values],
            "get_running_instances": lambda client: {1, 2},
            "get_next_session_name": lambda: ("cm", False),
            "run_tmux": self.record_tmux,
            "exec_tmux": lambda args: self.commands.append(["exec"] + args),
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(self.cm, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        env_patcher = mock.patch.dict(self.cm.os.environ, {"TMUX": ""})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def record_tmux(self, args: list[str], **kwargs):
        self.commands.append(args)
        return types.SimpleNamespace(returncode=0)

    def expected_shell_command(self, instance: int) -> str:
        cm_path = shlex.quote(str(self.cm.SCRIPT_DIR / "cm.py"))
        return f"{cm_path} ssh {instance} || exec $SHELL"

    def test_pan_quotes_cm_path_in_tmux_shell_commands(self) -> None:
        args = types.SimpleNamespace(instances=["1", "2"], sync=False)

        self.cm.cmd_panes(args)

        self.assertEqual(self.commands[0][-1], self.expected_shell_command(1))
        self.assertEqual(self.commands[5][-1], self.expected_shell_command(2))

    def test_win_quotes_cm_path_in_tmux_shell_commands(self) -> None:
        args = types.SimpleNamespace(instances=["1", "2"], sync=False)

        self.cm.cmd_win(args)

        self.assertEqual(self.commands[0][-1], self.expected_shell_command(1))
        self.assertEqual(self.commands[5][-1], self.expected_shell_command(2))

    def test_installed_wrapper_is_used_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            script_dir = Path(temp_dir)
            (script_dir / "cm").touch()

            with mock.patch.object(self.cm, "SCRIPT_DIR", script_dir):
                self.assertEqual(self.cm.get_cm_command_path(), script_dir / "cm")


if __name__ == "__main__":
    unittest.main()
