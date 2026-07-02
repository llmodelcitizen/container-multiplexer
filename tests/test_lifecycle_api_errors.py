from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeAPIError, FakeClient, FakeContainer, load_cm


class LifecycleAPIErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_lifecycle_api_error_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"

    def capture_stdout(self, func, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
            result = func(*args)
        return result, stdout.getvalue()

    def test_start_existing_container_reports_api_error_and_port_hint(self) -> None:
        container = FakeContainer("cm-001", status="exited")
        container.start = mock.Mock(side_effect=FakeAPIError("port is already allocated"))
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.start_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("Failed to start instance 1: port is already allocated", output)
        self.assertIn("cm rm 1; cm start 1", output)

    def test_stop_reports_api_error_without_traceback(self) -> None:
        container = FakeContainer("cm-001", status="running")
        container.stop = mock.Mock(side_effect=FakeAPIError("stop failed"))
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.stop_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("Failed to stop instance 1: stop failed", output)

    def test_restart_reports_stop_api_error_without_traceback(self) -> None:
        container = FakeContainer("cm-001", status="running")
        container.stop = mock.Mock(side_effect=FakeAPIError("stop failed"))
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.restart_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("Failed to restart instance 1: stop failed", output)

    def test_remove_reports_api_error_without_traceback(self) -> None:
        container = FakeContainer("cm-001", status="exited")
        container.remove = mock.Mock(side_effect=FakeAPIError("remove failed"))
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.rm_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("Failed to remove instance 1: remove failed", output)

    def test_stop_accepts_restarting_container(self) -> None:
        container = FakeContainer("cm-001", status="restarting")
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.stop_instance, client, 1)

        self.assertTrue(result)
        self.assertEqual(container.stopped, 1)
        self.assertIn("Stopped instance 1", output)

    def test_restart_stops_restarting_container_before_starting(self) -> None:
        container = FakeContainer("cm-001", status="restarting")
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.restart_instance, client, 1)

        self.assertTrue(result)
        self.assertEqual(container.stopped, 1)
        self.assertEqual(container.started, 1)
        self.assertIn("Started instance 1 (existing container)", output)

    def test_remove_forces_restarting_container(self) -> None:
        registry: dict[str, FakeContainer] = {}
        container = FakeContainer("cm-001", status="restarting", registry=registry)
        registry["cm-001"] = container
        client = FakeClient(registry)

        result, output = self.capture_stdout(self.cm.rm_instance, client, 1)

        self.assertTrue(result)
        self.assertEqual(container.remove_calls, [{"force": True}])
        self.assertNotIn("cm-001", registry)
        self.assertIn("Removed instance 1 (forced from restarting state)", output)


if __name__ == "__main__":
    unittest.main()
