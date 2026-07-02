from __future__ import annotations

import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import FakeClient, load_cm


class CleanCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_clean_test")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cm.WORKSPACES_DIR = Path(self.temp_dir.name) / "workspaces"

    def run_clean(self, client: FakeClient, input_effect="y"):
        stdout = io.StringIO()
        original_get_client = self.cm.get_client
        self.cm.get_client = lambda: client
        try:
            with contextlib.redirect_stdout(stdout), mock.patch(
                "builtins.input",
                side_effect=input_effect if isinstance(input_effect, list) else None,
                return_value=input_effect if not isinstance(input_effect, list) else None,
            ):
                result = self.cm.cmd_clean(types.SimpleNamespace())
        finally:
            self.cm.get_client = original_get_client
        return result, stdout.getvalue()

    def test_clean_removes_only_orphaned_cm_workspaces_after_confirmation(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        live = self.cm.WORKSPACES_DIR / "cm.001"
        orphan = self.cm.WORKSPACES_DIR / "cm.002"
        invalid = self.cm.WORKSPACES_DIR / "cm.bad"
        other = self.cm.WORKSPACES_DIR / "notes"
        for path in (live, orphan, invalid, other):
            path.mkdir()
        client = FakeClient(summaries=[{"Names": ["/cm-001"], "State": "exited"}])

        result, output = self.run_clean(client, "y")

        self.assertEqual(result, 0)
        self.assertTrue(live.exists())
        self.assertFalse(orphan.exists())
        self.assertTrue(invalid.exists())
        self.assertTrue(other.exists())
        self.assertIn("Orphaned workspaces (1):", output)
        self.assertIn("Removed cm.002/", output)

    def test_clean_preserves_workspace_referenced_by_update_backup(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        backup_workspace = self.cm.WORKSPACES_DIR / "cm.001"
        orphan = self.cm.WORKSPACES_DIR / "cm.002"
        backup_workspace.mkdir()
        orphan.mkdir()
        client = FakeClient(
            summaries=[
                {
                    "Names": ["/cm-update-backup-001-123-456"],
                    "State": "exited",
                    "Mounts": [
                        {
                            "Source": str(backup_workspace),
                            "Destination": "/home/me/workspace",
                        }
                    ],
                }
            ]
        )

        result, output = self.run_clean(client, "y")

        self.assertEqual(result, 0)
        self.assertTrue(backup_workspace.exists())
        self.assertFalse(orphan.exists())
        self.assertIn("Orphaned workspaces (1):", output)
        self.assertNotIn("cm.001/", output)
        self.assertIn("Removed cm.002/", output)

    def test_clean_ignores_lookalike_names_but_offers_exact_orphans(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        orphan = self.cm.WORKSPACES_DIR / "cm.002"
        lookalike_dirs = [
            self.cm.WORKSPACES_DIR / "cm.001.bak",
            self.cm.WORKSPACES_DIR / "cm.5",
            self.cm.WORKSPACES_DIR / "cm.000",
            self.cm.WORKSPACES_DIR / "cm.500",
            self.cm.WORKSPACES_DIR / "scratch",
            # Non-ASCII digits and a trailing newline must not slip past the
            # "exactly cm.NNN" filter (int() would otherwise parse them).
            self.cm.WORKSPACES_DIR / "cm.٠٠١",
            self.cm.WORKSPACES_DIR / "cm.001\n",
        ]
        plain_file = self.cm.WORKSPACES_DIR / "cm.003"
        for path in [orphan, *lookalike_dirs]:
            path.mkdir()
        plain_file.write_text("not a directory\n")

        result, output = self.run_clean(FakeClient(), "y")

        self.assertEqual(result, 0)
        self.assertFalse(orphan.exists())
        for path in lookalike_dirs:
            self.assertTrue(path.exists())
            self.assertNotIn(f"{path.name}/", output)
        self.assertTrue(plain_file.exists())
        self.assertIn("Orphaned workspaces (1):", output)
        self.assertIn("Removed cm.002/", output)

    def test_clean_lookalike_name_does_not_shadow_real_workspace(self) -> None:
        # cm.001.bak must not be protected by (or mistaken for) container cm-001.
        self.cm.WORKSPACES_DIR.mkdir()
        live = self.cm.WORKSPACES_DIR / "cm.001"
        lookalike = self.cm.WORKSPACES_DIR / "cm.001.bak"
        live.mkdir()
        lookalike.mkdir()
        client = FakeClient(summaries=[{"Names": ["/cm-001"], "State": "exited"}])

        result, output = self.run_clean(client, "y")

        self.assertEqual(result, 0)
        self.assertTrue(live.exists())
        self.assertTrue(lookalike.exists())
        self.assertIn("No orphaned workspaces found", output)

    def test_clean_aborts_without_removing_orphans_when_prompt_declines(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        orphan = self.cm.WORKSPACES_DIR / "cm.002"
        orphan.mkdir()

        result, output = self.run_clean(FakeClient(), "n")

        self.assertEqual(result, 1)
        self.assertTrue(orphan.exists())
        self.assertIn("Aborted", output)

    def test_clean_handles_eof_or_interrupt_as_abort(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        orphan = self.cm.WORKSPACES_DIR / "cm.002"
        orphan.mkdir()

        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error=error.__name__):
                orphan.mkdir(exist_ok=True)
                result, output = self.run_clean(FakeClient(), [error()])

                self.assertEqual(result, 1)
                self.assertTrue(orphan.exists())
                self.assertIn("  cm.002/", output)

    def test_clean_continues_after_workspace_removal_error(self) -> None:
        self.cm.WORKSPACES_DIR.mkdir()
        blocked = self.cm.WORKSPACES_DIR / "cm.001"
        removable = self.cm.WORKSPACES_DIR / "cm.002"
        blocked.mkdir()
        removable.mkdir()
        real_rmtree = self.cm.shutil.rmtree

        def fake_rmtree(path):
            if Path(path) == blocked:
                raise PermissionError("permission denied")
            return real_rmtree(path)

        with mock.patch.object(self.cm.shutil, "rmtree", side_effect=fake_rmtree):
            result, output = self.run_clean(FakeClient(), "y")

        self.assertEqual(result, 1)
        self.assertTrue(blocked.exists())
        self.assertFalse(removable.exists())
        self.assertIn("Failed to remove cm.001/", output)
        self.assertIn("Removed cm.002/", output)

    def test_clean_reports_missing_or_non_directory_workspace_root(self) -> None:
        missing_result, missing_output = self.run_clean(FakeClient(), "y")
        self.assertEqual(missing_result, 0)
        self.assertIn("No orphaned workspaces found", missing_output)

        self.cm.WORKSPACES_DIR.write_text("not a directory\n")
        file_result, file_output = self.run_clean(FakeClient(), "y")
        self.assertEqual(file_result, 1)
        self.assertIn("workspace path is not a directory", file_output)


if __name__ == "__main__":
    unittest.main()
