from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_cm():
    loader = importlib.machinery.SourceFileLoader("cm_collision_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNotFound(Exception):
    pass


class FakeAPIError(Exception):
    pass


class FakeContainer:
    def __init__(self, labels=None, status="running"):
        self.attrs = {"Config": {"Labels": labels or {}}}
        self.status = status
        self.started = 0
        self.stopped = 0
        self.removed = 0

    def start(self):
        self.started += 1
        self.status = "running"

    def stop(self):
        self.stopped += 1
        self.status = "exited"

    def remove(self, **kwargs):
        self.removed += 1


class FakeContainers:
    def __init__(self, container=None):
        self.container = container
        self.get_names = []

    def get(self, name):
        self.get_names.append(name)
        if self.container is None:
            raise FakeNotFound(name)
        return self.container


class FakeImages:
    def __init__(self):
        self.get_names = []

    def get(self, name):
        self.get_names.append(name)
        return object()


class FakeClient:
    def __init__(self, container=None):
        self.containers = FakeContainers(container)
        self.images = FakeImages()

    def close(self):
        pass


class ManagedContainerCollisionTests(unittest.TestCase):
    def setUp(self):
        self.cm = load_cm()
        self.cm._docker_sdk = types.SimpleNamespace(
            errors=types.SimpleNamespace(
                APIError=FakeAPIError,
                DockerException=Exception,
                NotFound=FakeNotFound,
            )
        )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"

    def run_command(self, func, args, client):
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: client
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                result = func(args)
        finally:
            self.cm.get_client = original_get_client
        return result, stdout.getvalue()

    def unmanaged_error(self):
        return "Docker name 'cm-001' is occupied by an unmanaged container"

    def test_direct_commands_refuse_unmanaged_container_name_collision(self):
        cases = [
            ("start", self.cm.cmd_start, types.SimpleNamespace(instances=["1"]), "exited"),
            ("stop", self.cm.cmd_stop, types.SimpleNamespace(instances=["1"]), "running"),
            ("restart", self.cm.cmd_restart, types.SimpleNamespace(instances=["1"]), "running"),
            (
                "update",
                self.cm.cmd_update,
                types.SimpleNamespace(instances=["1"], yes=True, force=False),
                "running",
            ),
            ("rm", self.cm.cmd_rm, types.SimpleNamespace(instances=["1"]), "exited"),
            ("ssh", self.cm.cmd_ssh, types.SimpleNamespace(instance=1, identity="/missing/key"), "running"),
            (
                "inspect",
                self.cm.cmd_inspect,
                types.SimpleNamespace(instance=1, no_exec=False, verbose=False, logs=10),
                "running",
            ),
        ]

        for name, func, args, status in cases:
            with self.subTest(command=name):
                container = FakeContainer(status=status)
                client = FakeClient(container)

                result, output = self.run_command(func, args, client)

                self.assertEqual(result, 1)
                self.assertIn(self.unmanaged_error(), output)
                self.assertEqual(container.started, 0)
                self.assertEqual(container.stopped, 0)
                self.assertEqual(container.removed, 0)
                self.assertEqual(client.images.get_names, [])

    def test_managed_container_actions_still_use_existing_container(self):
        managed = {"cm.managed": "true"}
        cases = [
            ("start", self.cm.cmd_start, types.SimpleNamespace(instances=["1"]), "exited", "started"),
            ("stop", self.cm.cmd_stop, types.SimpleNamespace(instances=["1"]), "running", "stopped"),
            ("restart", self.cm.cmd_restart, types.SimpleNamespace(instances=["1"]), "running", "restarted"),
            ("rm", self.cm.cmd_rm, types.SimpleNamespace(instances=["1"]), "exited", "removed"),
        ]

        for name, func, args, status, expected in cases:
            with self.subTest(command=name):
                container = FakeContainer(labels=managed, status=status)
                client = FakeClient(container)

                result, output = self.run_command(func, args, client)

                self.assertEqual(result, 0)
                self.assertIn("instance 1", output.lower())
                if expected == "started":
                    self.assertEqual(container.started, 1)
                    self.assertEqual(container.stopped, 0)
                    self.assertEqual(container.removed, 0)
                elif expected == "stopped":
                    self.assertEqual(container.started, 0)
                    self.assertEqual(container.stopped, 1)
                    self.assertEqual(container.removed, 0)
                elif expected == "restarted":
                    self.assertEqual(container.started, 1)
                    self.assertEqual(container.stopped, 1)
                    self.assertEqual(container.removed, 0)
                elif expected == "removed":
                    self.assertEqual(container.started, 0)
                    self.assertEqual(container.stopped, 0)
                    self.assertEqual(container.removed, 1)


if __name__ == "__main__":
    unittest.main()
