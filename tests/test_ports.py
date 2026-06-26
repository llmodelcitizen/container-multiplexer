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
    loader = importlib.machinery.SourceFileLoader("cm_under_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cm = load_cm()


class FakeNotFound(Exception):
    pass


class FakeAPIError(Exception):
    pass


cm._docker_sdk = types.SimpleNamespace(
    errors=types.SimpleNamespace(
        APIError=FakeAPIError,
        DockerException=Exception,
        NotFound=FakeNotFound,
    )
)


class FakeContainer:
    def __init__(self, attrs=None, status="running"):
        self.attrs = attrs or {}
        self.status = status


class FakeImage:
    def __init__(self, image_id: str | None = "sha256:aaaaaaaaaaaaaaaa"):
        self.id = image_id
        self.attrs = {}


class FakeImages:
    def __init__(self, image_id: str | None = "sha256:aaaaaaaaaaaaaaaa", exc=None):
        self.image_id = image_id
        self.exc = exc
        self.names = []

    def get(self, name):
        self.names.append(name)
        if self.exc:
            raise self.exc
        return FakeImage(self.image_id)


class FakeContainers:
    def __init__(self, container=None, exc=None):
        self.container = container
        self.exc = exc
        self.names = []

    def get(self, name):
        self.names.append(name)
        if self.exc:
            raise self.exc
        return self.container


class FakeAPI:
    def __init__(self, summaries):
        self.summaries = summaries

    def containers(self, **kwargs):
        return self.summaries


class FakeClient:
    def __init__(
        self,
        container=None,
        summaries=None,
        exc=None,
        image_id: str | None = "sha256:aaaaaaaaaaaaaaaa",
        image_exc=None,
    ):
        self.containers = FakeContainers(container, exc)
        self.api = FakeAPI(summaries or [])
        self.images = FakeImages(image_id, image_exc)


class PortLookupTests(unittest.TestCase):
    def run_cmd_list(self, client):
        original_get_client = cm.get_client
        cm.get_client = lambda: client
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = cm.cmd_list(types.SimpleNamespace())
        finally:
            cm.get_client = original_get_client
        return result, stdout.getvalue()

    def test_parse_status_extracts_healthy_healthcheck(self):
        self.assertEqual(
            cm.parse_status("Up 2 minutes (healthy)"),
            ("2 minutes", "healthy"),
        )

    def test_parse_status_extracts_unhealthy_healthcheck(self):
        self.assertEqual(
            cm.parse_status("Up 2 minutes (unhealthy)"),
            ("2 minutes", "unhealthy"),
        )

    def test_parse_status_extracts_starting_healthcheck(self):
        self.assertEqual(
            cm.parse_status("Up 4 seconds (health: starting)"),
            ("4 seconds", "starting"),
        )

    def test_parse_status_ignores_non_up_statuses(self):
        self.assertEqual(
            cm.parse_status("Restarting (1) 5 seconds ago"),
            ("-", "-"),
        )

    def test_summary_port_uses_actual_public_port(self):
        summary = {
            "Ports": [
                {"PrivatePort": 80, "PublicPort": 8080, "Type": "tcp"},
                {"PrivatePort": 22, "PublicPort": 2301, "Type": "tcp"},
            ]
        }

        self.assertEqual(cm.get_ssh_port_from_summary(summary), 2301)

    def test_summary_port_ignores_invalid_public_port(self):
        summary = {
            "Ports": [
                {"PrivatePort": 22, "PublicPort": 0, "Type": "tcp"},
                {"PrivatePort": 22, "PublicPort": 70000, "Type": "tcp"},
            ]
        }

        self.assertIsNone(cm.get_ssh_port_from_summary(summary))

    def test_container_attrs_use_network_settings_port(self):
        attrs = {
            "NetworkSettings": {
                "Ports": {
                    "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2401"}]
                }
            },
            "HostConfig": {
                "PortBindings": {
                    "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2501"}]
                }
            },
        }

        self.assertEqual(cm.get_ssh_port_from_attrs(attrs), 2401)

    def test_container_attrs_fall_back_to_host_config_port(self):
        attrs = {
            "NetworkSettings": {"Ports": {}},
            "HostConfig": {
                "PortBindings": {
                    "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2501"}]
                }
            },
        }

        self.assertEqual(cm.get_ssh_port_from_attrs(attrs), 2501)

    def test_container_port_returns_fallback_when_mapping_is_missing(self):
        container = FakeContainer(attrs={"NetworkSettings": {"Ports": {}}})

        self.assertEqual(cm.get_container_ssh_port(container, 2201), 2201)

    def test_list_port_inspects_when_summary_mapping_is_missing(self):
        container = FakeContainer(
            attrs={
                "HostConfig": {
                    "PortBindings": {
                        "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2601"}]
                    }
                }
            }
        )
        client = FakeClient(container=container)
        summary = {"Names": ["/cm-001"], "Ports": []}

        self.assertEqual(cm.get_list_ssh_port(client, summary, 2201), 2601)
        self.assertEqual(client.containers.names, ["cm-001"])

    def test_list_port_uses_fallback_when_inspect_fails(self):
        client = FakeClient(exc=FakeAPIError("inspect failed"))
        summary = {"Names": ["/cm-001"], "Ports": []}

        self.assertEqual(cm.get_list_ssh_port(client, summary, 2201), 2201)

    def test_cmd_list_prints_image_header_current_status_and_actual_summary_port(self):
        client = FakeClient(
            summaries=[
                {
                    "Names": ["/cm-001"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "ImageID": "sha256:aaaaaaaaaaaaaaaa",
                    "Ports": [
                        {"PrivatePort": 22, "PublicPort": 2301, "Type": "tcp"}
                    ],
                }
            ]
        )
        result, output = self.run_cmd_list(client)

        self.assertEqual(result, 0)
        self.assertIn("Image", output.splitlines()[0])
        self.assertIn("current", output)
        self.assertIn("2301", output)
        self.assertNotIn("2201", output)

    def test_cmd_list_prints_stale_image_status(self):
        client = FakeClient(
            summaries=[
                {
                    "Names": ["/cm-001"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "ImageID": "sha256:bbbbbbbbbbbbbbbb",
                    "Ports": [
                        {"PrivatePort": 22, "PublicPort": 2301, "Type": "tcp"}
                    ],
                },
                {
                    "Names": ["/cm-002"],
                    "State": "running",
                    "Status": "Up 2 minutes",
                    "ImageID": "sha256:bbbbbbbbbbbbbbbb",
                    "Ports": [
                        {"PrivatePort": 22, "PublicPort": 2302, "Type": "tcp"}
                    ],
                },
            ]
        )

        result, output = self.run_cmd_list(client)

        self.assertEqual(result, 0)
        self.assertEqual(output.count("stale"), 2)
        self.assertEqual(client.images.names, [cm.CM_IMAGE_REF])
        self.assertEqual(client.containers.names, [])

    def test_cmd_list_prints_unknown_when_container_image_id_is_missing(self):
        client = FakeClient(
            summaries=[
                {
                    "Names": ["/cm-001"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "Ports": [
                        {"PrivatePort": 22, "PublicPort": 2301, "Type": "tcp"}
                    ],
                }
            ]
        )

        result, output = self.run_cmd_list(client)

        self.assertEqual(result, 0)
        self.assertIn("unknown", output)

    def test_cmd_list_prints_unknown_when_local_image_id_is_missing(self):
        client = FakeClient(
            image_id=None,
            summaries=[
                {
                    "Names": ["/cm-001"],
                    "State": "running",
                    "Status": "Up 1 minute",
                    "ImageID": "sha256:aaaaaaaaaaaaaaaa",
                    "Ports": [
                        {"PrivatePort": 22, "PublicPort": 2301, "Type": "tcp"}
                    ],
                }
            ]
        )

        result, output = self.run_cmd_list(client)

        self.assertEqual(result, 0)
        self.assertIn("unknown", output)

    def test_cmd_ssh_uses_actual_container_port(self):
        container = FakeContainer(
            attrs={
                "Config": {"Labels": {"cm.managed": "true"}},
                "NetworkSettings": {
                    "Ports": {
                        "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2401"}]
                    }
                }
            }
        )
        client = FakeClient(container=container)
        original_get_client = cm.get_client
        original_execlp = cm.os.execlp
        cm.get_client = lambda: client

        class ExecCalled(Exception):
            pass

        def fake_execlp(*args):
            raise ExecCalled(args)

        cm.os.execlp = fake_execlp
        with tempfile.TemporaryDirectory() as temp_dir:
            identity = Path(temp_dir) / "cm_ed25519"
            identity.write_text("fake key")
            try:
                with self.assertRaises(ExecCalled) as raised:
                    cm.cmd_ssh(
                        types.SimpleNamespace(instance=1, identity=str(identity))
                    )
            finally:
                cm.get_client = original_get_client
                cm.os.execlp = original_execlp

        self.assertIn("-p", raised.exception.args[0])
        port_arg_index = raised.exception.args[0].index("-p") + 1
        self.assertEqual(raised.exception.args[0][port_arg_index], "2401")

    def test_port_allocation_error_matches_docker_desktop_message(self):
        error = FakeAPIError(
            "500 Server Error: ports are not available: exposing port TCP "
            "127.0.0.1:2201 -> 127.0.0.1:0: listen tcp4 127.0.0.1:2201: "
            "bind: address already in use"
        )

        self.assertTrue(cm.is_port_allocation_error(error))

    def test_port_allocation_error_matches_classic_message(self):
        error = FakeAPIError("port is already allocated")

        self.assertTrue(cm.is_port_allocation_error(error))

    def test_port_allocation_error_rejects_unrelated_api_error(self):
        error = FakeAPIError("image not found")

        self.assertFalse(cm.is_port_allocation_error(error))


if __name__ == "__main__":
    unittest.main()
