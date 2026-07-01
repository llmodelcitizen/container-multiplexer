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
