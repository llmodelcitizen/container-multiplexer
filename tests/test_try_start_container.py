from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import (
    FakeAPIError,
    FakeClient,
    FakeContainer,
    make_failed_port_container_then_raise,
    managed_labels,
    load_cm,
)


class TryStartContainerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_try_start_container_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.auth_keys = Path(self.temp_dir.name) / "authorized_keys"
        self.auth_keys.write_text("ssh-ed25519 fake\n")
        self.workspace = Path(self.temp_dir.name) / "cm.001"
        self.cfg = {
            "container": "cm-001",
            "port": 2201,
            "workspace": self.workspace,
        }

    def run_try_start(self, client: FakeClient):
        with mock.patch.object(self.cm, "get_authorized_keys_path", return_value=self.auth_keys), \
                mock.patch.object(self.cm, "get_container_environment", return_value=None):
            return self.cm.try_start_container(client, 1, self.cfg)

    def test_port_allocation_error_removes_failed_container_and_retries_next_port(self) -> None:
        registry: dict[str, FakeContainer] = {}
        failed_containers: list[FakeContainer] = []

        def create_failed_container_then_raise(containers, image, kwargs):
            container = containers.create_running(kwargs)
            failed_containers.append(container)
            raise FakeAPIError("port is already allocated")

        client = FakeClient(
            registry,
            run_effects=[
                create_failed_container_then_raise,
            ],
        )

        port, error = self.run_try_start(client)

        self.assertEqual((port, error), (2202, None))
        self.assertEqual(len(client.containers.run_calls), 2)
        first_kwargs = client.containers.run_calls[0][1]
        second_kwargs = client.containers.run_calls[1][1]
        self.assertEqual(first_kwargs["ports"], {"22/tcp": ("127.0.0.1", 2201)})
        self.assertEqual(second_kwargs["ports"], {"22/tcp": ("127.0.0.1", 2202)})
        self.assertEqual(registry["cm-001"].status, "running")
        self.assertEqual(failed_containers[0].removed, 1)
        self.assertEqual(failed_containers[0].remove_calls, [{"force": True}])

    def test_port_retry_returns_unmanaged_collision_before_incrementing(self) -> None:
        unmanaged = FakeContainer("cm-001", labels={}, status="created")
        registry = {"cm-001": unmanaged}
        client = FakeClient(
            registry,
            run_effects=[FakeAPIError("port is already allocated")],
        )

        port, error = self.run_try_start(client)

        self.assertIsNone(port)
        self.assertIn("unmanaged container", error)
        self.assertEqual(len(client.containers.run_calls), 1)
        self.assertEqual(unmanaged.removed, 0)

    def test_non_port_api_error_is_returned_without_retry(self) -> None:
        client = FakeClient(run_effects=[FakeAPIError("image pull failed")])

        port, error = self.run_try_start(client)

        self.assertIsNone(port)
        self.assertEqual(error, "image pull failed")
        self.assertEqual(len(client.containers.run_calls), 1)

    def test_non_port_api_error_removes_created_managed_container(self) -> None:
        created: list[FakeContainer] = []

        def create_container_then_raise(containers, image, kwargs):
            container = containers.create_running(kwargs)
            created.append(container)
            raise FakeAPIError("image pull failed")

        client = FakeClient(run_effects=[create_container_then_raise])

        port, error = self.run_try_start(client)

        self.assertIsNone(port)
        self.assertEqual(error, "image pull failed")
        self.assertEqual(created[0].remove_calls, [{"force": True}])
        self.assertNotIn("cm-001", client.registry)

    def test_retry_exhaustion_reports_after_100_attempts(self) -> None:
        effects = [
            make_failed_port_container_then_raise("bind: address already in use")
            for _ in range(100)
        ]
        client = FakeClient(run_effects=effects)

        port, error = self.run_try_start(client)

        self.assertIsNone(port)
        self.assertEqual(error, "Could not find available port after 100 attempts")
        self.assertEqual(len(client.containers.run_calls), 100)

    def test_run_kwargs_include_managed_label_authorized_keys_and_workspace_mounts(self) -> None:
        client = FakeClient()

        port, error = self.run_try_start(client)

        self.assertEqual((port, error), (2201, None))
        image, kwargs = client.containers.run_calls[0]
        self.assertEqual(image, self.cm.IMAGE_NAME)
        self.assertEqual(kwargs["labels"], managed_labels())
        self.assertEqual(
            kwargs["volumes"][str(self.auth_keys)],
            {"bind": self.cm.AUTHORIZED_KEYS_MOUNT, "mode": "ro"},
        )
        self.assertEqual(
            kwargs["volumes"][str(self.workspace)],
            {"bind": "/home/me/workspace", "mode": "rw"},
        )

    def test_run_kwargs_do_not_set_a_resurrecting_restart_policy(self) -> None:
        client = FakeClient()

        port, error = self.run_try_start(client)

        self.assertEqual((port, error), (2201, None))
        _image, kwargs = client.containers.run_calls[0]
        # Instances must not auto-restart on host boot (issue #55). Either no
        # restart policy at all (Docker default "no"), or an explicitly
        # non-resurrecting one.
        policy = kwargs.get("restart_policy")
        self.assertIn(
            policy,
            (None, {}, {"Name": ""}, {"Name": "no"}),
            f"unexpected restart_policy: {policy!r}",
        )


if __name__ == "__main__":
    unittest.main()
