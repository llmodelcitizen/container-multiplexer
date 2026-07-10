from __future__ import annotations

import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import FAKE_SSH_KEY, write_executable


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def run_uninstall(install_dir: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(install_dir.parent)
    env["CM_INSTALL_DIR"] = str(install_dir)
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstall"],
        input="",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def make_fake_python(fake_bin: Path) -> Path:
    python = fake_bin / "python3"
    write_executable(
        python,
        """#!/bin/sh
if [ "$1" = "-c" ]; then
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    venv_dir="$3"
    mkdir -p "$venv_dir/bin"
    cat > "$venv_dir/bin/python" <<'PYEOF'
#!/bin/sh
printf '%s\n' "$*" >> "$CM_FAKE_PIP_LOG"
exit 0
PYEOF
    chmod +x "$venv_dir/bin/python"
    exit 0
fi
printf 'unexpected fake python invocation: %s\n' "$*" >&2
exit 42
""",
    )
    return python


def make_fake_git(fake_bin: Path) -> Path:
    git = fake_bin / "git"
    write_executable(
        git,
        """#!/bin/sh
for arg in "$@"; do
    if [ "$arg" = "rev-parse" ]; then
        printf '.git\n'
        exit 0
    fi
    if [ "$arg" = "describe" ]; then
        printf '%s\n' "$CM_FAKE_GIT_VERSION"
        exit 0
    fi
done
exit 1
""",
    )
    return git


def run_install(home: Path, install_dir: Path, *, fake_python: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["CM_INSTALL_DIR"] = str(install_dir)
    if fake_python is not None:
        env["CM_PYTHON"] = str(fake_python)
    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        input="",
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


class InstallUninstallTests(unittest.TestCase):
    def test_install_creates_cm_home_venv_wrapper_authorized_keys_and_injects_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            home_ssh = home / ".ssh"
            home_ssh.mkdir(parents=True)
            (home_ssh / "cm_ed25519.pub").write_text("ssh-ed25519 fake-key\n", encoding="utf-8")
            pip_log = root / "pip.log"
            fake_python = make_fake_python(fake_bin)
            make_fake_git(fake_bin)

            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CM_INSTALL_DIR": str(install_dir),
                "CM_PYTHON": str(fake_python),
                "CM_FAKE_PIP_LOG": str(pip_log),
                "CM_FAKE_GIT_VERSION": "v1.2.3/feature&dirty",
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            })
            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                input="",
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (home / ".cm" / "authorized_keys").read_text(encoding="utf-8"),
                "ssh-ed25519 fake-key\n",
            )
            self.assertTrue((home / ".cm" / "workspaces").is_dir())
            self.assertTrue((install_dir / ".cm-venv" / "bin" / "python").is_file())
            self.assertIn("-m pip install --upgrade docker", pip_log.read_text(encoding="utf-8"))
            self.assertTrue((install_dir / "cm").is_file())
            self.assertIn(
                'exec "$SCRIPT_DIR/.cm-venv/bin/python" "$SCRIPT_DIR/cm.py" "$@"',
                (install_dir / "cm").read_text(encoding="utf-8"),
            )
            installed_script = (install_dir / "cm.py").read_text(encoding="utf-8")
            # '/' and '&' are stripped by version sanitization (issue #41).
            self.assertIn('VERSION = "v1.2.3featuredirty"', installed_script)
            self.assertIn("Version: v1.2.3featuredirty", result.stdout)
            self.assertIn("Installed successfully!", result.stdout)

    def test_install_sanitizes_version_containing_a_double_quote(self) -> None:
        # A git tag may legally contain '"', which would otherwise inject an
        # unterminated Python string literal into cm.py (issue #41).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            home_ssh = home / ".ssh"
            home_ssh.mkdir(parents=True)
            (home_ssh / "cm_ed25519.pub").write_text("ssh-ed25519 fake-key\n", encoding="utf-8")
            fake_python = make_fake_python(fake_bin)
            make_fake_git(fake_bin)

            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CM_INSTALL_DIR": str(install_dir),
                "CM_PYTHON": str(fake_python),
                "CM_FAKE_PIP_LOG": str(root / "pip.log"),
                "CM_FAKE_GIT_VERSION": 'v1.0"rc',
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            })
            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                input="",
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            installed_script = (install_dir / "cm.py").read_text(encoding="utf-8")
            self.assertIn('VERSION = "v1.0rc"', installed_script)
            self.assertNotIn('"dev"', installed_script)
            # The injected file must remain syntactically valid Python.
            ast.parse(installed_script)

    def test_install_rejects_python_older_than_39(self) -> None:
        # A python that reports < 3.9 must be refused before anything is built (issue #60).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            old_python = fake_bin / "python3"
            # Fail the version probe (`-c`) but otherwise behave like a python.
            write_executable(
                old_python,
                '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 1; fi\nexit 0\n',
            )

            result = run_install(home, install_dir, fake_python=old_python)

            self.assertEqual(result.returncode, 1)
            self.assertIn("Python 3.9 or newer", result.stdout)
            self.assertFalse((install_dir / ".cm-venv").exists())
            self.assertFalse((install_dir / "cm").exists())

    def test_install_errors_when_default_public_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            fake_python = make_fake_python(fake_bin)

            result = run_install(home, install_dir, fake_python=fake_python)

            self.assertEqual(result.returncode, 1)
            self.assertIn("cm_ed25519.pub was not found", result.stdout)
            self.assertFalse((install_dir / "cm").exists())

    def test_install_errors_when_cm_home_is_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            (home / ".cm").write_text("not a directory\n", encoding="utf-8")

            result = run_install(home, install_dir, fake_python=make_fake_python(fake_bin))

            self.assertEqual(result.returncode, 1)
            self.assertIn("exists and is not a directory", result.stdout)
            self.assertFalse((install_dir / "cm").exists())

    def test_install_errors_when_existing_authorized_keys_is_not_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            (home / ".cm" / "authorized_keys").mkdir(parents=True)

            result = run_install(home, install_dir, fake_python=make_fake_python(fake_bin))

            self.assertEqual(result.returncode, 1)
            self.assertIn("is not a file", result.stdout)
            self.assertFalse((install_dir / "cm").exists())

    def test_install_recreates_existing_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            stale = install_dir / ".cm-venv" / "stale"
            stale.parent.mkdir(parents=True)
            stale.write_text("old venv\n", encoding="utf-8")

            result = run_install(home, install_dir, fake_python=make_fake_python(fake_bin))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Removing existing Python virtual environment", result.stdout)
            self.assertFalse(stale.exists())
            self.assertTrue((install_dir / ".cm-venv" / "bin" / "python").is_file())

    def test_install_uses_default_directory_when_stdin_is_not_interactive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            pip_log = root / "pip.log"
            fake_python = make_fake_python(fake_bin)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CM_PYTHON": str(fake_python),
                "CM_FAKE_PIP_LOG": str(pip_log),
            })

            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            default_install_dir = home / ".local" / "bin"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Using default install directory", result.stdout)
            self.assertTrue((default_install_dir / "cm").is_file())
            self.assertFalse((root / 'INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"').exists())

    def test_install_expands_leading_tilde_slash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            fake_python = make_fake_python(fake_bin)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CM_INSTALL_DIR": "~/mybin",
                "CM_PYTHON": str(fake_python),
                "CM_FAKE_PIP_LOG": str(root / "pip.log"),
            })

            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                input="", text=True, capture_output=True, check=False, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / "mybin" / "cm").is_file())

    def test_install_rejects_user_tilde_path(self) -> None:
        # '~user/...' must be rejected, not mangled into '${HOME}user/...' (issue #42).
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            home.mkdir(parents=True)
            env = os.environ.copy()
            env.update({"HOME": str(home), "CM_INSTALL_DIR": "~eve/bin"})

            result = subprocess.run(
                ["bash", str(INSTALL_SH), "--uninstall"],
                input="", text=True, capture_output=True, check=False, env=env,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("'~user' home expansion is not supported", result.stderr)

    def test_install_normalizes_trailing_slash_and_skips_path_guidance(self) -> None:
        # A trailing slash must not defeat the PATH-membership check (issue #43).
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            home = root / "home"
            install_dir = root / "bin"
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True)
            (home / ".ssh").mkdir(parents=True)
            (home / ".ssh" / "cm_ed25519.pub").write_text(FAKE_SSH_KEY, encoding="utf-8")
            fake_python = make_fake_python(fake_bin)
            env = os.environ.copy()
            env.update({
                "HOME": str(home),
                "CM_INSTALL_DIR": f"{install_dir}/",
                "CM_PYTHON": str(fake_python),
                "CM_FAKE_PIP_LOG": str(root / "pip.log"),
                # install_dir (no trailing slash) is already on PATH.
                "PATH": f"{install_dir}{os.pathsep}{env['PATH']}",
            })

            result = subprocess.run(
                ["bash", str(INSTALL_SH)],
                input="", text=True, capture_output=True, check=False, env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((install_dir / "cm").is_file())
            self.assertNotIn("is not in your PATH", result.stdout)

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
