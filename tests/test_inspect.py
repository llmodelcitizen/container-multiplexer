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
    loader = importlib.machinery.SourceFileLoader("cm_inspect_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNotFound(Exception):
    pass


class FakeAPIError(Exception):
    pass


class FakeImage:
    def __init__(self, image_id: str | None, created: str | None = None):
        self.id = image_id
        self.attrs = {}
        if created:
            self.attrs["Created"] = created


class FakeImages:
    def __init__(
        self,
        image_id: str | None = "sha256:aaaaaaaaaaaaaaaa",
        created: str | None = None,
        images: dict[str, object] | None = None,
    ):
        self.image_id = image_id
        self.created = created
        self.images = images
        self.names = []

    def get(self, name):
        self.names.append(name)
        if self.images is not None and name in self.images:
            image = self.images[name]
            if isinstance(image, Exception):
                raise image
            return image
        return FakeImage(self.image_id, self.created)


class FakeContainer:
    def __init__(
        self,
        attrs: dict,
        status: str = "running",
        logs: bytes = b"line one\nline two\n",
        exec_error: Exception | None = None,
    ):
        self.attrs = attrs
        self.status = status
        self.logs_output = logs
        self.exec_error = exec_error
        self.exec_calls = []
        self.log_tails = []
        self.reloads = 0

    def reload(self):
        self.reloads += 1

    def exec_run(self, cmd, user=None):
        self.exec_calls.append((cmd, user))
        if self.exec_error:
            raise self.exec_error
        command = cmd[-1] if isinstance(cmd, list) else str(cmd)
        if "id me" in command:
            output = b"uid=1000(me) gid=1000(me) groups=1000(me)\n"
        elif "test -d /home/me/workspace" in command:
            output = b"writable"
        elif "stat -c" in command:
            output = b"me:me 600 90 bytes\n"
        else:
            output = b"ok\n"
        return types.SimpleNamespace(exit_code=0, output=output)

    def logs(self, tail=None):
        self.log_tails.append(tail)
        return self.logs_output


class FakeContainers:
    def __init__(self, container=None):
        self.container = container

    def get(self, name):
        if self.container is None:
            raise FakeNotFound(name)
        return self.container


class FakeClient:
    def __init__(
        self,
        container=None,
        image_id: str | None = "sha256:aaaaaaaaaaaaaaaa",
        image_created: str | None = None,
        images: dict[str, object] | None = None,
    ):
        self.containers = FakeContainers(container)
        self.images = FakeImages(image_id, image_created, images)


class InspectTests(unittest.TestCase):
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

    def attrs(self, workspace: Path, image_id: str = "sha256:aaaaaaaaaaaaaaaa"):
        return {
            "Id": "containerabcdef123456",
            "Image": image_id,
            "RestartCount": 1,
            "Created": "2026-06-26T12:00:00Z",
            "State": {
                "Status": "running",
                "StartedAt": "2026-06-26T12:01:00Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "ExitCode": 0,
                "Health": {
                    "Status": "healthy",
                    "Log": [
                        {
                            "End": "2026-06-26T12:02:00Z",
                            "ExitCode": 0,
                            "Output": "Connection to 127.0.0.1 22 port [tcp/ssh] succeeded!\n",
                        }
                    ],
                },
            },
            "Config": {
                "Image": "cm:latest",
                "Labels": {"cm.managed": "true"},
                "Env": ["CM_HOST_UID=1000", "CM_HOST_GID=1000"],
            },
            "NetworkSettings": {
                "Ports": {
                    "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": "2401"}]
                }
            },
            "Mounts": [
                {
                    "Source": str(workspace),
                    "Destination": "/home/me/workspace",
                    "Mode": "rw",
                    "RW": True,
                }
            ],
        }

    def run_inspect(self, container, client=None, **kwargs):
        args = types.SimpleNamespace(
            instance=kwargs.pop("instance", 1),
            no_exec=kwargs.pop("no_exec", False),
            verbose=kwargs.pop("verbose", False),
            logs=kwargs.pop("logs", 0),
        )
        self.assertFalse(kwargs)
        client = client or FakeClient(container)
        original_get_client = self.cm.get_client
        original_tcp = self.cm._check_host_tcp
        original_native_linux = self.cm.is_native_linux_host
        self.cm.get_client = lambda: client
        self.cm._check_host_tcp = lambda host, port: (True, f"{host}:{port} reachable")
        self.cm.is_native_linux_host = lambda: False
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                result = self.cm.cmd_inspect(args)
        finally:
            self.cm.get_client = original_get_client
            self.cm._check_host_tcp = original_tcp
            self.cm.is_native_linux_host = original_native_linux
        return result, stdout.getvalue()

    def test_cmd_inspect_reports_summary_and_runs_live_probes(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace))

        result, output = self.run_inspect(container)

        self.assertEqual(result, 0)
        self.assertIn("Instance 1: cm-001", output)
        self.assertIn("Published: 127.0.0.1:2401 -> 22/tcp", output)
        self.assertIn("Status: current", output)
        self.assertIn("ok   workspace bind mount is present", output)
        self.assertIn("ok   container image matches local cm:latest", output)
        self.assertIn("Live probes:", output)
        self.assertIn("workspace writable as me: writable", output)
        self.assertNotIn("disk usage:", output)
        self.assertEqual(len(container.exec_calls), 3)
        self.assertFalse(any(
            "df " in (cmd[-1] if isinstance(cmd, list) else str(cmd))
            for cmd, _user in container.exec_calls
        ))
        self.assertNotIn("Recent logs:", output)
        self.assertEqual(container.log_tails, [])

    def test_cmd_inspect_no_exec_skips_live_probes(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace))

        result, output = self.run_inspect(container, no_exec=True)

        self.assertEqual(result, 0)
        self.assertIn("skip --no-exec was passed", output)
        self.assertEqual(container.exec_calls, [])

    def test_cmd_inspect_warns_when_container_image_is_stale(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace, image_id="sha256:aaaaaaaaaaaaaaaa"))
        client = FakeClient(container, image_id="sha256:bbbbbbbbbbbbbbbb")

        result, output = self.run_inspect(container, client=client)

        self.assertEqual(result, 0)
        self.assertIn("Status: stale", output)
        self.assertIn("warn container image (cm:latest) differs from local cm:latest", output)

    def test_cmd_inspect_prints_image_creation_dates_when_available(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        image_id = "sha256:aaaaaaaaaaaaaaaa"
        container = FakeContainer(self.attrs(workspace, image_id=image_id))
        client = FakeClient(
            container,
            images={
                image_id: FakeImage(image_id, "2026-06-25T10:00:00Z"),
                self.cm.CM_IMAGE_REF: FakeImage(image_id, "2026-06-26T10:00:00Z"),
            },
        )

        result, output = self.run_inspect(container, client=client)

        self.assertEqual(result, 0)
        self.assertIn("Container image created: 2026-06-25T10:00:00Z", output)
        self.assertIn("Local cm:latest created: 2026-06-26T10:00:00Z", output)

    def test_cmd_inspect_reports_unknown_when_local_image_is_unavailable(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        image_id = "sha256:aaaaaaaaaaaaaaaa"
        container = FakeContainer(self.attrs(workspace, image_id=image_id))
        client = FakeClient(
            container,
            images={
                image_id: FakeImage(image_id, "2026-06-25T10:00:00Z"),
                self.cm.CM_IMAGE_REF: FakeAPIError("image missing"),
            },
        )

        result, output = self.run_inspect(container, client=client)

        self.assertEqual(result, 0)
        self.assertIn("Status: unknown", output)
        self.assertIn("Local cm:latest image ID: -", output)
        self.assertIn("warn local image cm:latest unavailable: image missing", output)

    def test_cmd_inspect_live_probe_failures_are_warnings(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace), exec_error=FakeAPIError("exec failed"))

        result, output = self.run_inspect(container)

        self.assertEqual(result, 0)
        self.assertIn("warn user me: exec failed: exec failed", output)

    def test_cmd_inspect_logs_zero_suppresses_log_lookup(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace))

        result, output = self.run_inspect(container, logs=0)

        self.assertEqual(result, 0)
        self.assertNotIn("Recent logs:", output)
        self.assertEqual(container.log_tails, [])

    def test_cmd_inspect_logs_option_includes_recent_logs(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace))

        result, output = self.run_inspect(container, logs=2)

        self.assertEqual(result, 0)
        self.assertIn("Recent logs:", output)
        self.assertIn("line two", output)
        self.assertEqual(container.log_tails, [2])

    def test_cmd_inspect_verbose_includes_docker_details(self):
        workspace = self.cm.WORKSPACES_DIR / "cm.001"
        workspace.mkdir(parents=True)
        container = FakeContainer(self.attrs(workspace))

        result, output = self.run_inspect(container, verbose=True)

        self.assertEqual(result, 0)
        self.assertIn("Verbose:", output)
        self.assertIn("Ports:", output)
        self.assertIn("Mounts:", output)
        self.assertIn("Environment:", output)
        self.assertIn("Health history:", output)

    def test_cmd_inspect_missing_container_returns_error(self):
        result, output = self.run_inspect(None, client=FakeClient(None))

        self.assertEqual(result, 1)
        self.assertIn("Instance 1 does not exist", output)

    def test_cmd_inspect_rejects_negative_log_count_before_docker(self):
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: self.fail("unexpected Docker call")
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                result = self.cm.cmd_inspect(
                    types.SimpleNamespace(
                        instance=1,
                        no_exec=False,
                        verbose=False,
                        logs=-1,
                    )
                )
        finally:
            self.cm.get_client = original_get_client

        self.assertEqual(result, 1)
        self.assertIn("--logs must be 0 or greater", stdout.getvalue())


class IdentityChecksTests(unittest.TestCase):
    def setUp(self):
        self.cm = load_cm()

    def _container(self, env):
        return types.SimpleNamespace(attrs={"Config": {"Env": env}})

    @contextlib.contextmanager
    def _host(self, uid, gid):
        import unittest.mock as mock

        with mock.patch.object(self.cm, "is_native_linux_host", lambda: True), \
                mock.patch.object(self.cm.os, "getuid", lambda: uid), \
                mock.patch.object(self.cm.os, "geteuid", lambda: uid), \
                mock.patch.object(self.cm.os, "getgid", lambda: gid):
            yield

    def test_default_container_ok_when_uid_and_gid_match_defaults(self):
        with self._host(uid=1000, gid=1000):
            checks = self.cm._identity_checks(self._container([]), 1)
        self.assertEqual(checks[0][0], "ok")

    def test_default_container_warns_when_gid_differs(self):
        # Host uid matches container default (1000) but gid differs: group
        # ownership of new workspace files will not match the host (issue #38).
        with self._host(uid=1000, gid=100):
            checks = self.cm._identity_checks(self._container([]), 1)
        status, message = checks[0]
        self.assertEqual(status, "warn")
        self.assertIn("group", message)
        self.assertIn("1000", message)
        self.assertIn("100", message)


if __name__ == "__main__":
    unittest.main()
