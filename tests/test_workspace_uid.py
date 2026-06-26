from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_cm():
    loader = importlib.machinery.SourceFileLoader("cm_workspace_under_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeAPIError(Exception):
    pass


class FakeNotFound(Exception):
    pass


class RecordingContainers:
    def __init__(self):
        self.image = None
        self.kwargs = None

    def run(self, image, **kwargs):
        self.image = image
        self.kwargs = kwargs


class FakeClient:
    def __init__(self):
        self.containers = RecordingContainers()


class WorkspaceUidTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm()
        self.cm._docker_sdk = types.SimpleNamespace(
            errors=types.SimpleNamespace(
                APIError=FakeAPIError,
                DockerException=Exception,
                NotFound=FakeNotFound,
            )
        )

    @contextlib.contextmanager
    def host_identity(self, platform: str, uid: int = 1234, gid: int = 2345, euid: int | None = None):
        with mock.patch.object(self.cm.sys, "platform", platform), \
                mock.patch.object(self.cm.os, "getuid", lambda: uid), \
                mock.patch.object(self.cm.os, "geteuid", lambda: uid if euid is None else euid), \
                mock.patch.object(self.cm.os, "getgid", lambda: gid):
            yield

    def test_new_linux_container_gets_host_uid_gid_environment(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_keys = Path(temp_dir) / "authorized_keys"
            auth_keys.write_text("ssh-ed25519 fake\n")
            cfg = {
                "container": "cm-001",
                "port": 2201,
                "workspace": Path(temp_dir) / "cm.001",
            }

            with self.host_identity("linux", uid=1234, gid=2345), \
                    mock.patch.object(self.cm, "get_authorized_keys_path", lambda: auth_keys):
                port, error = self.cm.try_start_container(client, 1, cfg)

        self.assertEqual((port, error), (2201, None))
        self.assertEqual(client.containers.image, self.cm.IMAGE_NAME)
        self.assertEqual(
            client.containers.kwargs["environment"],
            {"CM_HOST_UID": "1234", "CM_HOST_GID": "2345"},
        )

    def test_macos_container_creation_does_not_set_uid_gid_environment(self) -> None:
        client = FakeClient()
        with tempfile.TemporaryDirectory() as temp_dir:
            auth_keys = Path(temp_dir) / "authorized_keys"
            auth_keys.write_text("ssh-ed25519 fake\n")
            cfg = {
                "container": "cm-001",
                "port": 2201,
                "workspace": Path(temp_dir) / "cm.001",
            }

            with self.host_identity("darwin"), \
                    mock.patch.object(self.cm, "get_authorized_keys_path", lambda: auth_keys):
                port, error = self.cm.try_start_container(client, 1, cfg)

        self.assertEqual((port, error), (2201, None))
        self.assertNotIn("environment", client.containers.kwargs)

    def test_linux_root_preflight_fails_before_creating_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "cm.001"
            with self.host_identity("linux", uid=0, gid=0, euid=0):
                with self.assertRaises(self.cm.WorkspacePreflightError):
                    self.cm.prepare_workspace(workspace)

            self.assertFalse(workspace.exists())

    def test_linux_workspace_preflight_creates_and_probes_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "cm.001"
            with self.host_identity("linux", uid=1234, gid=2345):
                self.cm.prepare_workspace(workspace)

            self.assertTrue(workspace.is_dir())
            self.assertEqual(list(workspace.iterdir()), [])

    def test_cmd_start_rejects_linux_root_before_docker_client(self) -> None:
        with self.host_identity("linux", uid=0, gid=0, euid=0), \
                mock.patch.object(self.cm, "get_client", side_effect=AssertionError("unexpected Docker call")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = self.cm.cmd_start(types.SimpleNamespace(instances=["1"]))

        self.assertEqual(result, 1)
        self.assertIn("do not run cm start/restart with sudo", stderr.getvalue())

    def test_existing_container_without_identity_rejected_for_nondefault_linux_uid(self) -> None:
        container = types.SimpleNamespace(attrs={"Config": {"Env": []}})

        with self.host_identity("linux", uid=1234, gid=2345):
            error = self.cm.get_existing_container_identity_error(container, 1)

        self.assertIsNotNone(error)
        self.assertIn("1000:1000", error)
        self.assertIn("1234:2345", error)

    def test_existing_container_identity_accepts_match(self) -> None:
        container = types.SimpleNamespace(
            attrs={"Config": {"Env": ["CM_HOST_UID=1234", "CM_HOST_GID=2345"]}}
        )

        with self.host_identity("linux", uid=1234, gid=2345):
            self.assertIsNone(self.cm.get_existing_container_identity_error(container, 1))

    def test_existing_container_without_identity_accepts_default_uid(self) -> None:
        container = types.SimpleNamespace(attrs={"Config": {"Env": []}})

        with self.host_identity("linux", uid=1000, gid=1000):
            self.assertIsNone(self.cm.get_existing_container_identity_error(container, 1))


if __name__ == "__main__":
    unittest.main()
