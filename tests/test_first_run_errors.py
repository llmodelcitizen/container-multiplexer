from __future__ import annotations

import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeClient, FakeContainer, load_cm


class FirstRunErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_first_run_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name)
        self.cm.AUTHORIZED_KEYS_PATH = self.home / ".cm" / "authorized_keys"

    def running_client(self) -> FakeClient:
        registry = {
            "cm-001": FakeContainer(
                "cm-001",
                status="running",
                attrs={
                    "Config": {"Labels": {"cm.managed": "true"}, "Env": []},
                    "NetworkSettings": {
                        "Ports": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2301"}]}
                    },
                },
            )
        }
        return FakeClient(registry)

    def run_ssh(self, client: FakeClient, *, identity=None, access=True):
        stdout = io.StringIO()
        stderr = io.StringIO()
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: client
        args = types.SimpleNamespace(instance=1, identity=identity)
        patches = [
            mock.patch.dict("os.environ", {"HOME": str(self.home)}),
            mock.patch.object(self.cm.os, "access", lambda path, mode: access),
            mock.patch.object(self.cm.os, "execlp", side_effect=AssertionError("ssh should not run")),
        ]
        try:
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = self.cm.cmd_ssh(args)
        finally:
            self.cm.get_client = original_get_client
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cmd_ssh_reports_not_running_before_key_checks(self) -> None:
        registry = {"cm-001": FakeContainer("cm-001", status="exited")}

        result, stdout, stderr = self.run_ssh(FakeClient(registry))

        self.assertEqual(result, 1)
        self.assertIn("Instance 1 is not running", stdout)
        self.assertEqual(stderr, "")

    def test_cmd_ssh_expands_default_identity_and_reports_missing_key(self) -> None:
        result, _stdout, stderr = self.run_ssh(self.running_client(), identity=None)

        expected = self.home / ".ssh" / "cm_ed25519"
        self.assertEqual(result, 1)
        self.assertIn(f"SSH private key is not a file: {expected}", stderr)
        self.assertIn("ssh-keygen -t ed25519 -f ~/.ssh/cm_ed25519", stderr)

    def test_cmd_ssh_reports_unreadable_identity(self) -> None:
        key = self.home / "key"
        key.write_text("private\n")

        result, _stdout, stderr = self.run_ssh(self.running_client(), identity=str(key), access=False)

        self.assertEqual(result, 1)
        self.assertIn(f"Cannot read SSH private key: {key}", stderr)

    def capture_authorized_keys_exit(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as ctx:
            self.cm.get_authorized_keys_path()
        return ctx.exception, stderr.getvalue()

    def test_get_authorized_keys_path_exits_with_guidance_when_missing(self) -> None:
        exc, stderr = self.capture_authorized_keys_exit()

        self.assertEqual(exc.code, 1)
        self.assertIn("authorized_keys source is not a file", stderr)
        self.assertIn("./install.sh", stderr)

    def test_get_authorized_keys_path_exits_when_empty(self) -> None:
        self.cm.AUTHORIZED_KEYS_PATH.parent.mkdir(parents=True)
        self.cm.AUTHORIZED_KEYS_PATH.touch()

        exc, stderr = self.capture_authorized_keys_exit()

        self.assertEqual(exc.code, 1)
        self.assertIn("authorized_keys source is empty", stderr)

    def test_get_authorized_keys_path_exits_when_unreadable(self) -> None:
        self.cm.AUTHORIZED_KEYS_PATH.parent.mkdir(parents=True)
        self.cm.AUTHORIZED_KEYS_PATH.write_text("ssh-ed25519 fake\n")

        with mock.patch.object(self.cm.os, "access", return_value=False):
            exc, stderr = self.capture_authorized_keys_exit()

        self.assertEqual(exc.code, 1)
        self.assertIn("Cannot read authorized_keys source", stderr)


if __name__ == "__main__":
    unittest.main()
