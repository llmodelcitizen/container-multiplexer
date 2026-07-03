from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class EntrypointHarness:
    """Run entrypoint.sh behavior against a temporary filesystem and command shims."""

    def __init__(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="cm-entrypoint-")
        self.temp_path = Path(self.temp_dir.name)
        self.root = self.temp_path / "root"
        self.bin = self.temp_path / "bin"
        self.state = self.temp_path / "state"
        self.log = self.temp_path / "commands.log"
        self.home = self.root / "home" / "me"
        self.ssh_dir = self.home / ".ssh"
        self.workspace = self.home / "workspace"
        self.authorized_keys_src = self.root / "tmp" / "cm_authorized_keys"
        self.authorized_keys_dst = self.ssh_dir / "authorized_keys"

        for path in (
            self.root / "etc" / "profile.d",
            self.root / "tmp",
            self.home,
            self.workspace,
            self.bin,
            self.state / "groups_by_gid",
            self.state / "gids_by_group",
            self.state / "users_by_uid",
        ):
            path.mkdir(parents=True, exist_ok=True)

        (self.state / "uid").write_text("1000\n", encoding="utf-8")
        (self.state / "gid").write_text("1000\n", encoding="utf-8")
        (self.state / "group_name").write_text("me\n", encoding="utf-8")
        self.add_group("me", "1000")
        self.add_user("me", "1000")
        self._write_stubs()
        self.entrypoint = self._write_relocated_entrypoint()

    def cleanup(self) -> None:
        self.temp_dir.cleanup()

    def add_group(self, name: str, gid: str) -> None:
        (self.state / "groups_by_gid" / gid).write_text(f"{name}\n", encoding="utf-8")
        (self.state / "gids_by_group" / name).write_text(f"{gid}\n", encoding="utf-8")

    def add_user(self, name: str, uid: str) -> None:
        (self.state / "users_by_uid" / uid).write_text(f"{name}\n", encoding="utf-8")

    def write_authorized_keys(
        self,
        content: str = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICmTestKey cm\n",
    ) -> None:
        self.authorized_keys_src.write_text(content, encoding="utf-8")

    def state_value(self, name: str) -> str:
        return (self.state / name).read_text(encoding="utf-8").strip()

    def log_text(self) -> str:
        if not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8")

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        run_env = os.environ.copy()
        for name in ("CM_HOST_UID", "CM_HOST_GID", "CM_TEST_WORKSPACE_WRITABLE"):
            run_env.pop(name, None)
        run_env.update(
            {
                "CM_TEST_STATE": str(self.state),
                "CM_TEST_LOG": str(self.log),
                "PATH": f"{self.bin}{os.pathsep}{run_env.get('PATH', '')}",
            }
        )
        run_env.update(env)
        return subprocess.run(
            ["bash", str(self.entrypoint)],
            cwd=ROOT,
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _write_relocated_entrypoint(self) -> Path:
        script = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        replacements = {
            "/etc/profile.d/cm-path.sh": str(
                self.root / "etc" / "profile.d" / "cm-path.sh"
            ),
            "/tmp/cm_authorized_keys": str(self.authorized_keys_src),
            "/home/me": str(self.home),
        }
        for original, replacement in replacements.items():
            script = script.replace(original, replacement)
        script = script.replace(
            "exec /usr/sbin/sshd -D -e",
            f"exec {shlex.quote(str(self.bin / 'sshd'))} -D -e",
        )

        relocated = self.temp_path / "entrypoint.sh"
        relocated.write_text(script, encoding="utf-8")
        relocated.chmod(0o755)
        return relocated

    def _write_stub(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\nset -eu\n" + textwrap.dedent(body).lstrip(),
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _write_stubs(self) -> None:
        self._write_stub(
            "id",
            r"""
            printf 'id %s\n' "$*" >> "$CM_TEST_LOG"
            if [ "$#" -eq 2 ] && [ "$1" = "-u" ] && [ "$2" = "me" ]; then
                cat "$CM_TEST_STATE/uid"
                exit 0
            fi
            if [ "$#" -eq 2 ] && [ "$1" = "-g" ] && [ "$2" = "me" ]; then
                cat "$CM_TEST_STATE/gid"
                exit 0
            fi
            if [ "$#" -eq 2 ] && [ "$1" = "-gn" ] && [ "$2" = "me" ]; then
                cat "$CM_TEST_STATE/group_name"
                exit 0
            fi
            echo "unsupported id args: $*" >&2
            exit 64
            """,
        )
        self._write_stub(
            "getent",
            r"""
            printf 'getent %s\n' "$*" >> "$CM_TEST_LOG"
            if [ "$#" -ne 2 ]; then
                echo "unsupported getent args: $*" >&2
                exit 64
            fi
            if [ "$1" = "group" ]; then
                entry="$CM_TEST_STATE/groups_by_gid/$2"
                [ -f "$entry" ] || exit 2
                name="$(cat "$entry")"
                printf '%s:x:%s:\n' "$name" "$2"
                exit 0
            fi
            if [ "$1" = "passwd" ]; then
                entry="$CM_TEST_STATE/users_by_uid/$2"
                [ -f "$entry" ] || exit 2
                name="$(cat "$entry")"
                gid="$(cat "$CM_TEST_STATE/gid")"
                printf '%s:x:%s:%s::/home/%s:/bin/bash\n' "$name" "$2" "$gid" "$name"
                exit 0
            fi
            echo "unsupported getent database: $1" >&2
            exit 64
            """,
        )
        self._write_stub(
            "groupmod",
            r"""
            printf 'groupmod %s\n' "$*" >> "$CM_TEST_LOG"
            if [ "$#" -ne 3 ] || [ "$1" != "-g" ] || [ "$3" != "me" ]; then
                echo "unsupported groupmod args: $*" >&2
                exit 64
            fi
            new_gid="$2"
            existing="$CM_TEST_STATE/groups_by_gid/$new_gid"
            if [ -f "$existing" ] && [ "$(cat "$existing")" != "me" ]; then
                echo "groupmod: GID $new_gid already exists" >&2
                exit 4
            fi
            old_gid="$(cat "$CM_TEST_STATE/gids_by_group/me")"
            rm -f "$CM_TEST_STATE/groups_by_gid/$old_gid"
            printf 'me\n' > "$CM_TEST_STATE/groups_by_gid/$new_gid"
            printf '%s\n' "$new_gid" > "$CM_TEST_STATE/gids_by_group/me"
            printf '%s\n' "$new_gid" > "$CM_TEST_STATE/gid"
            printf 'me\n' > "$CM_TEST_STATE/group_name"
            """,
        )
        self._write_stub(
            "usermod",
            r"""
            printf 'usermod %s\n' "$*" >> "$CM_TEST_LOG"
            if [ "$#" -eq 3 ] && [ "$1" = "-g" ] && [ "$3" = "me" ]; then
                gid_file="$CM_TEST_STATE/gids_by_group/$2"
                [ -f "$gid_file" ] || {
                    echo "usermod: group $2 does not exist" >&2
                    exit 6
                }
                gid="$(cat "$gid_file")"
                printf '%s\n' "$gid" > "$CM_TEST_STATE/gid"
                printf '%s\n' "$2" > "$CM_TEST_STATE/group_name"
                exit 0
            fi
            if [ "$#" -eq 3 ] && [ "$1" = "-u" ] && [ "$3" = "me" ]; then
                new_uid="$2"
                existing="$CM_TEST_STATE/users_by_uid/$new_uid"
                if [ -f "$existing" ] && [ "$(cat "$existing")" != "me" ]; then
                    echo "usermod: UID $new_uid already exists" >&2
                    exit 4
                fi
                old_uid="$(cat "$CM_TEST_STATE/uid")"
                rm -f "$CM_TEST_STATE/users_by_uid/$old_uid"
                printf 'me\n' > "$CM_TEST_STATE/users_by_uid/$new_uid"
                printf '%s\n' "$new_uid" > "$CM_TEST_STATE/uid"
                exit 0
            fi
            echo "unsupported usermod args: $*" >&2
            exit 64
            """,
        )
        self._write_stub(
            "chown",
            r"""
            printf 'chown %s\n' "$*" >> "$CM_TEST_LOG"
            """,
        )
        self._write_stub(
            "install",
            r"""
            printf 'install %s\n' "$*" >> "$CM_TEST_LOG"
            directory=0
            mode=
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    -d)
                        directory=1
                        shift
                        ;;
                    -o|-g|-m)
                        if [ "$1" = "-m" ]; then
                            mode="$2"
                        fi
                        shift 2
                        ;;
                    *)
                        break
                        ;;
                esac
            done
            if [ "$directory" -eq 1 ]; then
                [ "$#" -eq 1 ] || {
                    echo "unsupported install directory args" >&2
                    exit 64
                }
                mkdir -p "$1"
                chmod "$mode" "$1"
                exit 0
            fi
            [ "$#" -eq 2 ] || {
                echo "unsupported install file args" >&2
                exit 64
            }
            mkdir -p "$(dirname "$2")"
            cp "$1" "$2"
            chmod "$mode" "$2"
            """,
        )
        self._write_stub(
            "sudo",
            r"""
            printf 'sudo %s\n' "$*" >> "$CM_TEST_LOG"
            if [ "$#" -eq 5 ] && \
                [ "$1" = "-u" ] && \
                [ "$2" = "me" ] && \
                [ "$3" = "test" ] && \
                [ "$4" = "-w" ]; then
                if [ "${CM_TEST_WORKSPACE_WRITABLE:-1}" = "1" ]; then
                    exit 0
                fi
                exit 1
            fi
            echo "unsupported sudo args: $*" >&2
            exit 64
            """,
        )
        self._write_stub(
            "sshd",
            r"""
            printf 'sshd %s\n' "$*" >> "$CM_TEST_LOG"
            """,
        )


class RuntimeImageTests(unittest.TestCase):
    def make_entrypoint_harness(self) -> EntrypointHarness:
        harness = EntrypointHarness()
        self.addCleanup(harness.cleanup)
        return harness

    def assert_log_order(self, log: str, *needles: str) -> None:
        position = -1
        for needle in needles:
            next_position = log.find(needle)
            self.assertNotEqual(
                next_position,
                -1,
                f"{needle!r} not found in log:\n{log}",
            )
            self.assertGreater(
                next_position,
                position,
                f"{needle!r} was out of order in log:\n{log}",
            )
            position = next_position

    def test_entrypoint_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "entrypoint.sh")],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entrypoint_remaps_user_installs_authorized_keys_and_starts_sshd(self):
        harness = self.make_entrypoint_harness()
        harness.write_authorized_keys()
        bashrc = harness.home / ".bashrc"
        bashrc.write_text("# keep me\n", encoding="utf-8")

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.state_value("uid"), "1234")
        self.assertEqual(harness.state_value("gid"), "2345")
        self.assertEqual(harness.state_value("group_name"), "me")
        self.assertEqual(
            (harness.root / "etc" / "profile.d" / "cm-path.sh").read_text(
                encoding="utf-8"
            ),
            'export PATH="$HOME/.local/bin:$PATH"\n',
        )
        self.assertEqual(bashrc.read_text(encoding="utf-8"), "# keep me\n")
        self.assertEqual(
            harness.authorized_keys_dst.read_text(encoding="utf-8"),
            harness.authorized_keys_src.read_text(encoding="utf-8"),
        )
        self.assertEqual(stat.S_IMODE(harness.ssh_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(harness.authorized_keys_dst.stat().st_mode),
            0o600,
        )

        log = harness.log_text()
        self.assert_log_order(
            log,
            "groupmod -g 2345 me",
            "usermod -u 1234 me",
            f"chown me:me {harness.home}",
            f"install -d -o me -g me -m 700 {harness.ssh_dir}",
            (
                "install -o me -g me -m 600 "
                f"{harness.authorized_keys_src} {harness.authorized_keys_dst}"
            ),
            f"sudo -u me test -w {harness.workspace}",
            "sshd -D -e",
        )

    def test_dockerfile_has_ssh_healthcheck(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("nc -z 127.0.0.1 22", dockerfile)

    def test_base_image_sets_default_me_uid_gid(self):
        dockerfile = (ROOT / "Dockerfile.base").read_text()

        self.assertIn("groupadd -g 1000 me", dockerfile)
        self.assertIn("useradd -m -u 1000 -g 1000 -s /bin/bash me", dockerfile)

    def test_entrypoint_switches_to_existing_group_on_gid_collision(self):
        harness = self.make_entrypoint_harness()
        harness.add_group("hostgroup", "2345")
        harness.write_authorized_keys()

        result = harness.run(CM_HOST_UID="1000", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.state_value("uid"), "1000")
        self.assertEqual(harness.state_value("gid"), "2345")
        self.assertEqual(harness.state_value("group_name"), "hostgroup")

        log = harness.log_text()
        self.assertIn("getent group 2345", log)
        self.assertIn("usermod -g hostgroup me", log)
        self.assertNotIn("groupmod -g 2345 me", log)
        self.assertIn(f"install -d -o me -g hostgroup -m 700 {harness.ssh_dir}", log)

    def test_entrypoint_rejects_uid_collision_before_usermod(self):
        harness = self.make_entrypoint_harness()
        harness.add_user("taken", "1234")
        harness.write_authorized_keys()

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="1000")

        self.assertEqual(result.returncode, 1)
        self.assertIn("UID is already used by taken", result.stderr)

        log = harness.log_text()
        self.assertIn("getent passwd 1234", log)
        self.assertNotIn("usermod -u 1234 me", log)
        self.assertNotIn("install -o", log)
        self.assertNotIn("sshd -D -e", log)
        self.assertFalse(harness.authorized_keys_dst.exists())

    def test_entrypoint_rejects_invalid_uid_gid_before_remapping(self):
        harness = self.make_entrypoint_harness()
        harness.write_authorized_keys()

        result = harness.run(CM_HOST_UID="0", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be positive integers", result.stderr)

        log = harness.log_text()
        self.assertNotIn("groupmod", log)
        self.assertNotIn("usermod", log)
        self.assertNotIn("install -o", log)
        self.assertNotIn("sshd -D -e", log)
        self.assertFalse(harness.authorized_keys_dst.exists())

    def test_entrypoint_rejects_unwritable_workspace_before_sshd(self):
        harness = self.make_entrypoint_harness()
        harness.write_authorized_keys()

        result = harness.run(
            CM_HOST_UID="1234",
            CM_HOST_GID="2345",
            CM_TEST_WORKSPACE_WRITABLE="0",
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn("workspace is not writable by user me", result.stderr)
        self.assertTrue(harness.authorized_keys_dst.exists())

        log = harness.log_text()
        self.assert_log_order(
            log,
            (
                "install -o me -g me -m 600 "
                f"{harness.authorized_keys_src} {harness.authorized_keys_dst}"
            ),
            f"sudo -u me test -w {harness.workspace}",
        )
        self.assertNotIn("sshd -D -e", log)

    def test_dockerfile_runtime_contract_order(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertLess(
            dockerfile.index("COPY --chmod=755 entrypoint.sh"),
            dockerfile.index("ENTRYPOINT"),
        )
        self.assertLess(dockerfile.index("HEALTHCHECK"), dockerfile.index("ENTRYPOINT"))


if __name__ == "__main__":
    unittest.main()
