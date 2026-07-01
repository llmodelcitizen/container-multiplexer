from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeAPIError, FakeClient, FakeContainer, load_cm


class UpdateFailureBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_update_failure_branch_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"
        self.cm.AUTHORIZED_KEYS_PATH = Path(self.temp_dir.name) / "authorized_keys"
        self.cm.AUTHORIZED_KEYS_PATH.write_text("ssh-ed25519 fake\n")

    def make_client(self, old_container: FakeContainer, *, run_error: Exception | None = None) -> FakeClient:
        registry = {old_container.name: old_container}
        old_container.registry = registry
        effects = [run_error] if run_error is not None else None
        return FakeClient(registry, image_id="sha256:new", run_effects=effects)

    def update_result(self, client: FakeClient):
        with mock.patch.object(self.cm, "is_native_linux_host", return_value=False):
            return self.cm._update_instance_result(
                client,
                1,
                force=False,
                local_image=self.cm.ImageMetadata("sha256:new"),
            )

    def test_update_reports_prepare_failure_and_restarts_running_container(self) -> None:
        old = FakeContainer("cm-001", status="running", image_id="sha256:old")
        client = self.make_client(old)
        old.rename = mock.Mock(side_effect=RuntimeError("rename failed"))

        success, stdout_messages, stderr_messages = self.update_result(client)

        self.assertFalse(success)
        self.assertEqual(stderr_messages, [])
        self.assertEqual(stdout_messages, ["Failed to prepare instance 1 for update: rename failed"])
        self.assertEqual(old.stopped, 1)
        self.assertEqual(old.started, 1)
        self.assertEqual(client.containers.run_calls, [])

    def test_update_reports_restore_failure_after_new_container_create_error(self) -> None:
        old = FakeContainer("cm-001", status="running", image_id="sha256:old")
        client = self.make_client(old, run_error=FakeAPIError("image pull failed"))
        original_rename = old.rename
        rename_calls = []

        def rename_once_then_fail(name):
            rename_calls.append(name)
            if len(rename_calls) == 2:
                raise RuntimeError("restore failed")
            return original_rename(name)

        old.rename = rename_once_then_fail

        success, stdout_messages, stderr_messages = self.update_result(client)

        self.assertFalse(success)
        self.assertEqual(stderr_messages, [])
        self.assertEqual(len(rename_calls), 2)
        self.assertIn(
            "Failed to update instance 1: image pull failed; also failed to restore old container: restore failed",
            stdout_messages,
        )

    def test_update_warns_when_backup_container_cannot_be_removed(self) -> None:
        old = FakeContainer("cm-001", status="running", image_id="sha256:old")
        client = self.make_client(old)

        def fail_remove(**kwargs):
            old.remove_calls.append(dict(kwargs))
            raise RuntimeError("remove failed")

        old.remove = fail_remove

        success, stdout_messages, stderr_messages = self.update_result(client)

        self.assertTrue(success)
        self.assertEqual(stderr_messages, [])
        self.assertIn(
            "Warning: updated instance 1 but could not remove backup",
            "\n".join(stdout_messages),
        )
        self.assertIn("remove failed", "\n".join(stdout_messages))
        self.assertIn("Updated instance 1 from stale image", "\n".join(stdout_messages))
        self.assertEqual(old.remove_calls, [{"force": True}])


if __name__ == "__main__":
    unittest.main()
