from __future__ import annotations

import subprocess
import types
import unittest
from unittest import mock

from tests.support import FakeDockerException, load_cm


class FakeDockerClient:
    def __init__(self, *, ping_error: Exception | None = None):
        self.ping_error = ping_error
        self.closed = 0

    def ping(self):
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def close(self):
        self.closed += 1


class FakeDockerSDK:
    def __init__(
        self,
        *,
        from_env_error: Exception | None = None,
        context_client: FakeDockerClient | Exception | None = None,
    ):
        self.errors = types.SimpleNamespace(DockerException=FakeDockerException)
        self.from_env_error = from_env_error
        self.context_client = context_client or FakeDockerClient()
        self.context_base_urls: list[str] = []

    def from_env(self):
        if self.from_env_error is not None:
            raise self.from_env_error
        return FakeDockerClient()

    def DockerClient(self, *, base_url: str):
        self.context_base_urls.append(base_url)
        if isinstance(self.context_client, Exception):
            raise self.context_client
        return self.context_client


class DockerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_docker_client_test")

    def test_get_client_uses_docker_cli_context_host_when_default_socket_fails(self) -> None:
        sdk = FakeDockerSDK(from_env_error=FakeDockerException("default socket missing"))
        self.cm._docker_sdk = sdk
        context = subprocess.CompletedProcess(
            ["docker"],
            0,
            stdout='"unix:///tmp/context.sock"\n',
            stderr="",
        )

        with mock.patch.dict(self.cm.os.environ, {}, clear=True), \
                mock.patch.object(self.cm.subprocess, "run", return_value=context):
            client = self.cm.get_client()

        self.assertIs(client, sdk.context_client)
        self.assertEqual(sdk.context_base_urls, ["unix:///tmp/context.sock"])

    def test_get_client_error_mentions_docker_host_when_context_is_unavailable(self) -> None:
        self.cm._docker_sdk = FakeDockerSDK(
            from_env_error=FakeDockerException("default socket missing")
        )
        context = subprocess.CompletedProcess(["docker"], 1, stdout="", stderr="no context\n")

        with mock.patch.dict(self.cm.os.environ, {}, clear=True), \
                mock.patch.object(self.cm.subprocess, "run", return_value=context), \
                self.assertRaises(SystemExit) as ctx:
            self.cm.get_client()

        message = str(ctx.exception)
        self.assertIn("Cannot connect to Docker with the Python Docker SDK", message)
        self.assertIn("DOCKER_HOST", message)
        self.assertIn("docker info", message)
        self.assertIn("default socket missing", message)
