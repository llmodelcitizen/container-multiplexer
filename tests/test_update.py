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
    loader = importlib.machinery.SourceFileLoader("cm_update_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNotFound(Exception):
    pass


class FakeAPIError(Exception):
    pass


class FakeImage:
    def __init__(self, image_id: str | None = "sha256:bbbbbbbbbbbbbbbb"):
        self.id = image_id
        self.attrs = {}


class FakeImages:
    def __init__(self, image_id: str | None = "sha256:bbbbbbbbbbbbbbbb"):
        self.image_id = image_id
        self.names = []

    def get(self, name):
        self.names.append(name)
        return FakeImage(self.image_id)


class FakeContainer:
    def __init__(
        self,
        name: str,
        image_id: str | None,
        status: str,
        registry: dict[str, "FakeContainer"],
    ):
        self.name = name
        self.status = status
        self.registry = registry
        self.attrs = {
            "Image": image_id,
            "State": {"Status": status},
            "Config": {"Labels": {"cm.managed": "true"}, "Env": []},
        }
        self.started = 0
        self.stopped = 0
        self.removed = 0
        self.renames: list[str] = []

    def start(self):
        self.started += 1
        self.status = "running"
        self.attrs["State"]["Status"] = "running"

    def stop(self):
        self.stopped += 1
        self.status = "exited"
        self.attrs["State"]["Status"] = "exited"

    def rename(self, name):
        self.registry.pop(self.name, None)
        self.name = name
        self.renames.append(name)
        self.registry[name] = self

    def remove(self, **kwargs):
        self.removed += 1
        self.registry.pop(self.name, None)


class FakeContainers:
    def __init__(
        self,
        registry: dict[str, FakeContainer],
        latest_image_id: str | None,
        run_error: Exception | None = None,
    ):
        self.registry = registry
        self.latest_image_id = latest_image_id
        self.run_error = run_error
        self.run_calls = []
        self.get_names = []

    def get(self, name):
        self.get_names.append(name)
        if name not in self.registry:
            raise FakeNotFound(name)
        return self.registry[name]

    def run(self, image, **kwargs):
        self.run_calls.append((image, kwargs))
        if self.run_error:
            raise self.run_error
        container = FakeContainer(
            kwargs["name"],
            self.latest_image_id,
            "running",
            self.registry,
        )
        self.registry[kwargs["name"]] = container
        return container


class FakeAPI:
    def __init__(self, registry: dict[str, FakeContainer]):
        self.registry = registry

    def containers(self, **kwargs):
        summaries = []
        for name, container in self.registry.items():
            summaries.append({
                "Names": [f"/{name}"],
                "State": container.status,
                "ImageID": container.attrs.get("Image"),
            })
        return summaries


class FakeClient:
    def __init__(
        self,
        registry: dict[str, FakeContainer],
        image_id: str | None = "sha256:bbbbbbbbbbbbbbbb",
        run_error: Exception | None = None,
    ):
        self.registry = registry
        self.images = FakeImages(image_id)
        self.containers = FakeContainers(registry, image_id, run_error)
        self.api = FakeAPI(registry)


class UpdateTests(unittest.TestCase):
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
        self.cm.AUTHORIZED_KEYS_PATH = Path(self.temp_dir.name) / "authorized_keys"
        self.cm.AUTHORIZED_KEYS_PATH.write_text("ssh-ed25519 fake\n")

    def make_container(
        self,
        registry: dict[str, FakeContainer],
        n: int = 1,
        image_id: str | None = "sha256:aaaaaaaaaaaaaaaa",
        status: str = "running",
    ) -> FakeContainer:
        name = f"cm-{n:03d}"
        container = FakeContainer(name, image_id, status, registry)
        registry[name] = container
        return container

    def run_update(
        self,
        client: FakeClient,
        *,
        instances=None,
        yes: bool = True,
        force: bool = False,
        input_value: str | None = None,
    ):
        args = types.SimpleNamespace(
            instances=instances or ["1"],
            yes=yes,
            force=force,
        )
        original_get_client = self.cm.get_client
        original_native_linux = self.cm.is_native_linux_host
        self.cm.get_client = lambda: client
        self.cm.is_native_linux_host = lambda: False
        stdout = io.StringIO()
        input_patch = (
            mock.patch("builtins.input", return_value=input_value)
            if input_value is not None else contextlib.nullcontext()
        )
        try:
            with contextlib.redirect_stdout(stdout), input_patch:
                result = self.cm.cmd_update(args)
        finally:
            self.cm.get_client = original_get_client
            self.cm.is_native_linux_host = original_native_linux
        return result, stdout.getvalue()

    def test_update_stale_running_container_recreates_from_latest(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry, status="running")
        client = FakeClient(registry)

        result, output = self.run_update(client)

        self.assertEqual(result, 0)
        self.assertIn("Warning: recreating instance(s)", output)
        self.assertIn("Updated instance 1 from stale image", output)
        self.assertEqual(old_container.stopped, 1)
        self.assertEqual(old_container.removed, 1)
        self.assertTrue(old_container.renames[0].startswith("cm-update-backup-001-"))
        new_container = registry["cm-001"]
        self.assertIsNot(new_container, old_container)
        self.assertEqual(new_container.attrs["Image"], "sha256:bbbbbbbbbbbbbbbb")
        self.assertEqual(new_container.status, "running")
        self.assertEqual(client.containers.run_calls[0][0], self.cm.IMAGE_NAME)

    def test_update_stopped_container_remains_stopped(self):
        registry: dict[str, FakeContainer] = {}
        self.make_container(registry, status="exited")
        client = FakeClient(registry)

        result, _output = self.run_update(client)

        self.assertEqual(result, 0)
        self.assertEqual(registry["cm-001"].status, "exited")
        self.assertEqual(registry["cm-001"].stopped, 1)

    def test_update_current_container_skips_without_force(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry, image_id="sha256:bbbbbbbbbbbbbbbb")
        client = FakeClient(registry)

        result, output = self.run_update(client)

        self.assertEqual(result, 0)
        self.assertIn("image is current", output)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(old_container.renames, [])

    def test_update_force_recreates_current_container(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry, image_id="sha256:bbbbbbbbbbbbbbbb")
        client = FakeClient(registry)

        result, output = self.run_update(client, force=True)

        self.assertEqual(result, 0)
        self.assertIn("Updated instance 1 from current image", output)
        self.assertEqual(len(client.containers.run_calls), 1)
        self.assertEqual(old_container.removed, 1)

    def test_update_prompts_and_aborts_without_yes(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry)
        client = FakeClient(registry)

        result, output = self.run_update(client, yes=False, input_value="n")

        self.assertEqual(result, 1)
        self.assertIn("Warning: recreating instance(s)", output)
        self.assertIn("Aborted", output)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(old_container.renames, [])

    def test_update_failure_restores_old_running_container(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry, status="running")
        client = FakeClient(registry, run_error=FakeAPIError("bad image"))

        result, output = self.run_update(client)

        self.assertEqual(result, 1)
        self.assertIn("restored old container", output)
        self.assertIs(registry["cm-001"], old_container)
        self.assertEqual(old_container.status, "running")
        self.assertEqual(old_container.started, 1)
        self.assertEqual(old_container.removed, 0)

    def test_update_all_updates_only_stale_instances_by_default(self):
        registry: dict[str, FakeContainer] = {}
        stale = self.make_container(registry, n=1)
        current = self.make_container(
            registry,
            n=2,
            image_id="sha256:bbbbbbbbbbbbbbbb",
        )
        client = FakeClient(registry)

        result, output = self.run_update(client, instances=["all"])

        self.assertEqual(result, 0)
        self.assertIn("1 (stale)", output)
        self.assertNotIn("2 (current)", output)
        self.assertEqual(stale.removed, 1)
        self.assertEqual(current.renames, [])
        self.assertEqual(len(client.containers.run_calls), 1)

    def test_update_unknown_requires_force(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry, image_id=None)
        client = FakeClient(registry)

        result, output = self.run_update(client)

        self.assertEqual(result, 1)
        self.assertIn("image status is unknown", output)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(old_container.renames, [])

    def test_update_requires_local_image_id(self):
        registry: dict[str, FakeContainer] = {}
        old_container = self.make_container(registry)
        client = FakeClient(registry, image_id=None)

        result, output = self.run_update(client)

        self.assertEqual(result, 1)
        self.assertIn("has no readable image ID", output)
        self.assertIn("docker build -t cm-base:latest -f Dockerfile.base .", output)
        self.assertIn("docker build -t cm:latest .", output)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(old_container.renames, [])


if __name__ == "__main__":
    unittest.main()
