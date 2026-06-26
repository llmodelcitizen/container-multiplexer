from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def run_uninstall(install_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(install_dir.parent)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstall"],
        input=f"{install_dir}\n",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


class InstallUninstallTests(unittest.TestCase):
    def test_uninstall_removes_files_and_venv_then_prints_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "bin"
            install_dir.mkdir()
            (install_dir / "cm").write_text("#!/bin/sh\n", encoding="utf-8")
            (install_dir / "cm.py").write_text("print('cm')\n", encoding="utf-8")
            venv_bin = install_dir / ".cm-venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python").write_text("", encoding="utf-8")

            result = run_uninstall(install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((install_dir / "cm").exists())
            self.assertFalse((install_dir / "cm.py").exists())
            self.assertFalse((install_dir / ".cm-venv").exists())
            self.assertIn("Uninstalled successfully!", result.stdout)
            self.assertNotIn("Nothing to remove", result.stdout)

    def test_uninstall_prints_nothing_to_remove_when_targets_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "bin"
            install_dir.mkdir()

            result = run_uninstall(install_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Nothing to remove in {install_dir}", result.stdout)
            self.assertNotIn("Uninstalled successfully!", result.stdout)


if __name__ == "__main__":
    unittest.main()
