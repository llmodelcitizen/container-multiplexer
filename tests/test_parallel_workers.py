from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeClient, FakeContainer, FakeNotFound, load_cm


class ParallelWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_parallel_worker_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"
        self.cm.AUTHORIZED_KEYS_PATH = Path(self.temp_dir.name) / "authorized_keys"
        self.cm.AUTHORIZED_KEYS_PATH.write_text("ssh-ed25519 fake\n")

    def run_worker(self, worker, client: FakeClient, *args):
        with mock.patch.object(self.cm, "get_client", return_value=client), \
                mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
            return worker(*args)

    def test_start_worker_creates_missing_container_and_closes_client(self) -> None:
        client = FakeClient()

        n, success, message = self.run_worker(self.cm._start_instance_worker, client, 1)

        self.assertEqual((n, success), (1, True))
        self.assertIn("Started instance 1 (port 2201", message)
        self.assertIn("cm-001", client.registry)
        self.assertEqual(client.containers.run_calls[0][0], self.cm.IMAGE_NAME)
        self.assertEqual(client.closed, 1)

    def test_start_worker_reuses_stopped_managed_container(self) -> None:
        old = FakeContainer("cm-001", status="exited")
        client = FakeClient({"cm-001": old})

        n, success, message = self.run_worker(self.cm._start_instance_worker, client, 1)

        self.assertEqual((n, success, message), (1, True, "Started instance 1 (existing container)"))
        self.assertEqual(old.started, 1)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(client.closed, 1)

    def test_start_worker_reports_missing_image(self) -> None:
        client = FakeClient(image_exc=FakeNotFound("cm"))

        n, success, message = self.run_worker(self.cm._start_instance_worker, client, 1)

        self.assertEqual((n, success), (1, False))
        self.assertIn("Image 'cm:latest' not found.", message)
        self.assertEqual(client.containers.run_calls, [])
        self.assertEqual(client.closed, 1)

    def test_stop_worker_stops_running_container_and_rejects_missing_one(self) -> None:
        running = FakeContainer("cm-001", status="running")
        client = FakeClient({"cm-001": running})

        self.assertEqual(
            self.run_worker(self.cm._stop_instance_worker, client, 1),
            (1, True, "Stopped instance 1"),
        )
        self.assertEqual(running.stopped, 1)
        self.assertEqual(client.closed, 1)

        missing = FakeClient()
        self.assertEqual(
            self.run_worker(self.cm._stop_instance_worker, missing, 2),
            (2, False, "Instance 2 does not exist"),
        )
        self.assertEqual(missing.closed, 1)

    def test_restart_worker_restarts_existing_running_container(self) -> None:
        running = FakeContainer("cm-001", status="running")
        client = FakeClient({"cm-001": running})

        n, success, message = self.run_worker(self.cm._restart_instance_worker, client, 1)

        self.assertEqual((n, success, message), (1, True, "Restarted instance 1 (existing container)"))
        self.assertEqual(running.stopped, 1)
        self.assertEqual(running.started, 1)
        self.assertEqual(client.closed, 1)

    def test_restart_worker_creates_missing_container(self) -> None:
        client = FakeClient()

        n, success, message = self.run_worker(self.cm._restart_instance_worker, client, 1)

        self.assertEqual((n, success), (1, True))
        self.assertIn("Restarted instance 1 (port 2201", message)
        self.assertIn("cm-001", client.registry)
        self.assertEqual(client.closed, 1)

    def test_rm_worker_removes_dead_container_and_rejects_running_one(self) -> None:
        dead = FakeContainer("cm-001", status="exited")
        client = FakeClient({"cm-001": dead})

        self.assertEqual(
            self.run_worker(self.cm._rm_instance_worker, client, 1),
            (1, True, "Removed instance 1"),
        )
        self.assertEqual(dead.removed, 1)
        self.assertEqual(client.closed, 1)

        running = FakeContainer("cm-002", status="running")
        running_client = FakeClient({"cm-002": running})
        self.assertEqual(
            self.run_worker(self.cm._rm_instance_worker, running_client, 2),
            (2, False, "Instance 2 is running (use 'stop' instead)"),
        )
        self.assertEqual(running.removed, 0)
        self.assertEqual(running_client.closed, 1)

    def test_multi_instance_commands_dispatch_to_parallel_workers(self) -> None:
        cases = [
            (self.cm.cmd_start, types.SimpleNamespace(instances=["1", "2"]), self.cm._start_instance_worker),
            (self.cm.cmd_stop, types.SimpleNamespace(instances=["1", "2"]), self.cm._stop_instance_worker),
            (self.cm.cmd_restart, types.SimpleNamespace(instances=["1", "2"]), self.cm._restart_instance_worker),
            (self.cm.cmd_rm, types.SimpleNamespace(instances=["1", "2"]), self.cm._rm_instance_worker),
        ]

        for func, args, expected_worker in cases:
            with self.subTest(command=func.__name__):
                calls = []

                def fake_run_parallel(worker, instances):
                    calls.append((worker, instances))
                    return True

                with mock.patch.object(self.cm, "run_parallel", fake_run_parallel), \
                        mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
                    result = func(args)

                self.assertEqual(result, 0)
                self.assertEqual(calls, [(expected_worker, [1, 2])])


if __name__ == "__main__":
    unittest.main()
