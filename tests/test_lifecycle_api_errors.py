from __future__ import annotations

import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FAKE_SSH_KEY, FakeAPIError, FakeClient, FakeContainer, load_cm


class LifecycleAPIErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_lifecycle_api_error_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"
        self.cm.AUTHORIZED_KEYS_PATH = Path(self.temp_dir.name) / "authorized_keys"
        self.cm.AUTHORIZED_KEYS_PATH.write_text(FAKE_SSH_KEY)

    def capture_stdout(self, func, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
            result = func(*args)
        return result, stdout.getvalue()

    def capture_output(self, func, *args):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
            result = func(*args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_start_existing_container_reports_api_error_and_port_hint(self) -> None:
        container = FakeContainer("cm-001", status="exited")
        container.start = mock.Mock(side_effect=FakeAPIError("port is already allocated"))
        client = FakeClient({"cm-001": container})

        result, output = self.capture_stdout(self.cm.start_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("Failed to start instance 1: port is already allocated", output)
        self.assertIn("cm rm 1; cm start 1", output)

    def test_start_existing_container_validates_authorized_keys_first(self) -> None:
        container = FakeContainer("cm-001", status="exited")
        client = FakeClient({"cm-001": container})
        self.cm.AUTHORIZED_KEYS_PATH = Path(self.temp_dir.name) / "missing_authorized_keys"

        result, _stdout, stderr = self.capture_output(self.cm.start_instance, client, 1)

        self.assertFalse(result)
        self.assertEqual(container.started, 0)
        self.assertIn("authorized_keys source is not a file", stderr)

    def test_start_routes_unmanaged_collision_to_stderr(self) -> None:
        # Unmanaged name collisions go to stderr, matching the neighboring
        # preflight error and the parallel path's error stream.
        container = FakeContainer("cm-001", status="exited", labels={})
        client = FakeClient({"cm-001": container})

        result, stdout, stderr = self.capture_output(self.cm.start_instance, client, 1)

        self.assertFalse(result)
        self.assertIn("unmanaged container", stderr)
        self.assertNotIn("unmanaged container", stdout)

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
        self.assertIn("Restarted instance 1 (existing container)", output)

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

    def _run_cmd_rm(self, client, instances):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(self.cm, "get_client", return_value=client), \
                mock.patch.object(self.cm, "is_native_linux_host", return_value=False), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.cm.cmd_rm(types.SimpleNamespace(instances=instances))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cmd_rm_omits_workspace_note_for_running_instance(self) -> None:
        workspace = self.cm.get_instance_config(1)["workspace"]
        workspace.mkdir(parents=True)
        registry: dict[str, FakeContainer] = {}
        container = FakeContainer("cm-001", status="running", registry=registry)
        registry["cm-001"] = container
        client = FakeClient(registry)

        result, stdout, stderr = self._run_cmd_rm(client, ["1"])

        combined = stdout + stderr
        self.assertEqual(result, 1)
        self.assertIn("is running (use 'stop' instead)", combined)
        self.assertNotIn("remain on disk", combined)

    def test_cmd_rm_notes_workspace_for_removed_instance(self) -> None:
        workspace = self.cm.get_instance_config(1)["workspace"]
        workspace.mkdir(parents=True)
        registry: dict[str, FakeContainer] = {}
        container = FakeContainer("cm-001", status="exited", registry=registry)
        registry["cm-001"] = container
        client = FakeClient(registry)

        result, stdout, stderr = self._run_cmd_rm(client, ["1"])

        combined = stdout + stderr
        self.assertEqual(result, 0)
        self.assertIn("Removed instance 1", combined)
        self.assertIn("remain on disk", combined)
        self.assertIn(str(workspace), combined)

    def test_cmd_rm_notes_workspace_for_nonexistent_instance(self) -> None:
        # No container, but an orphaned workspace dir remains -> note is valid.
        workspace = self.cm.get_instance_config(1)["workspace"]
        workspace.mkdir(parents=True)
        client = FakeClient({})

        result, stdout, stderr = self._run_cmd_rm(client, ["1"])

        combined = stdout + stderr
        self.assertEqual(result, 1)
        self.assertIn("Instance 1 does not exist", combined)
        self.assertIn("remain on disk", combined)
