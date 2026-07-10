from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.support import (
    FAKE_SSH_KEY,
    authorized_keys_mount,
    authorized_keys_src_env,
    write_executable,
)


ROOT = Path(__file__).resolve().parents[1]

# An absolute-path literal: a slash-anchored token that is not part of a word
# (login/interactive), a variable-relative path ($HOME/.local/bin), or a
# relative path (./x, ../x).
ABS_PATH_LITERAL_RE = re.compile(r"(?<![\w.])/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+")


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
        self.authorized_keys_src = self.root / authorized_keys_mount().lstrip("/")
        self.authorized_keys_dst = self.ssh_dir / "authorized_keys"

        for path in (
            self.root / "etc" / "profile.d",
            self.authorized_keys_src.parent,
            self.home,
            self.workspace,
            self.bin,
            self.state / "groups_by_gid",
            self.state / "users_by_uid",
        ):
            path.mkdir(parents=True, exist_ok=True)

        self.log.touch()
        (self.state / "gid").write_text("1000\n", encoding="utf-8")
        self.add_group("me", "1000")
        self.add_user("me", "1000")
        self.authorized_keys_src.write_text(FAKE_SSH_KEY, encoding="utf-8")
        self._write_stubs()
        self.entrypoint = self._write_relocated_entrypoint()

    def cleanup(self) -> None:
        self.temp_dir.cleanup()

    def add_group(self, name: str, gid: str) -> None:
        (self.state / "groups_by_gid" / gid).write_text(f"{name}\n", encoding="utf-8")

    def add_user(self, name: str, uid: str) -> None:
        (self.state / "users_by_uid" / uid).write_text(f"{name}\n", encoding="utf-8")

    def state_value(self, name: str) -> str:
        return (self.state / name).read_text(encoding="utf-8").strip()

    def group_name(self) -> str:
        return self.state_value(f"groups_by_gid/{self.state_value('gid')}")

    def uid(self) -> str:
        matches = [
            entry.name
            for entry in (self.state / "users_by_uid").iterdir()
            if entry.read_text(encoding="utf-8").strip() == "me"
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one users_by_uid entry for me, found: {matches}"
            )
        return matches[0]

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8")

    def install_keys_log_line(self) -> str:
        """Expected authorized_keys install log line; call after run()."""
        return (
            f"install -o me -g {self.group_name()} -m 600 "
            f"{self.authorized_keys_src} {self.authorized_keys_dst}"
        )

    def workspace_probe_log_line(self) -> str:
        """Expected workspace writability probe log line; call after run()."""
        return f"sudo -u me test -w {self.workspace}"

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        run_env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "CM_TEST_STATE": str(self.state),
            "CM_TEST_LOG": str(self.log),
            # Mirror cm.py's get_container_environment(): the entrypoint is
            # told where authorized_keys is mounted instead of hardcoding it.
            authorized_keys_src_env(): str(self.authorized_keys_src),
            **env,
        }
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
        # Every value is shlex-quoted so relocated paths survive shell-special
        # characters (e.g. a space in TMPDIR). Keys whose only occurrences sit
        # inside double quotes in entrypoint.sh include those quotes, so the
        # quoted value replaces the whole shell word instead of nesting a
        # literal layer of single quotes inside the double quotes. The bare
        # "/home/me" key covers the remaining unquoted uses (chown targets and
        # -f tests), where a quoted prefix concatenates into one shell word.
        replacements = {
            '"/home/me/.ssh/authorized_keys"': shlex.quote(
                str(self.authorized_keys_dst)
            ),
            '"/home/me/.ssh"': shlex.quote(str(self.ssh_dir)),
            '"/home/me/workspace"': shlex.quote(str(self.workspace)),
            "/etc/profile.d/cm-path.sh": shlex.quote(
                str(self.root / "etc" / "profile.d" / "cm-path.sh")
            ),
            "/home/me": shlex.quote(str(self.home)),
            "exec /usr/sbin/sshd -D -e": (
                f"exec {shlex.quote(str(self.bin / 'sshd'))} -D -e"
            ),
        }
        for original in replacements:
            if original not in script:
                raise AssertionError(
                    f"entrypoint.sh no longer contains {original!r}; the "
                    "harness relocates that container path into a temp dir. "
                    "Update this replacement to match the current "
                    "entrypoint.sh, and keep the runtime contract between "
                    "cm.py and entrypoint.sh intact."
                )
        # Substitute in a single pass so replaced text is never rescanned (a
        # temp path containing "/home/me" must not be rewritten again). Longer
        # keys come first in the alternation so e.g.
        # '"/home/me/.ssh/authorized_keys"' wins over '"/home/me/.ssh"' when
        # both match at the same position.
        pattern = re.compile(
            "|".join(
                re.escape(original)
                for original in sorted(replacements, key=len, reverse=True)
            )
        )
        script = pattern.sub(lambda match: replacements[match.group(0)], script)
        self._relocated_values = tuple(replacements.values())
        self._assert_relocation_complete(script)

        relocated = self.temp_path / "entrypoint.sh"
        write_executable(relocated, script)
        return relocated

    def _assert_relocation_complete(self, script: str) -> None:
        """Fail when an absolute-path literal escaped relocation.

        After substitution, every absolute path in the relocated script must
        point inside the harness temp root; the only other legitimate
        absolute is the shebang. Anything else would make the entrypoint
        operate on the real filesystem while the tests pass.
        """
        body = script.split("\n", 1)[1] if script.startswith("#!") else script
        temp_root = f"{self.temp_path}/"
        # Relocated values may be shlex-quoted ('<temp path>' when TMPDIR has
        # shell-special characters), and suffixed uses concatenate after the
        # closing quote (e.g. '<home>'/.bashrc). Collapse the exact inserted
        # values to a word token — no quote parsing, which prose apostrophes
        # in script comments would defeat — so neither a quoted path nor its
        # concatenated suffix is misread as an escaped absolute path.
        for value in sorted(self._relocated_values, key=len, reverse=True):
            body = body.replace(value, "__RELOCATED__")
        escaped = sorted(
            path
            for path in set(ABS_PATH_LITERAL_RE.findall(body))
            if not path.startswith(temp_root)
            # entrypoint.sh keeps cm.py's mount path as the manual-run
            # fallback; run() always overrides it via AUTHORIZED_KEYS_SRC_ENV,
            # and test_entrypoint_default_matches_cm_authorized_keys_mount
            # pins it to cm.py's constant.
            and path != authorized_keys_mount()
        )
        if escaped:
            raise AssertionError(
                "absolute paths escaped relocation: "
                + ", ".join(escaped)
                + "; add replacements in EntrypointHarness"
            )

    def _write_stub(self, name: str, body: str) -> None:
        write_executable(
            self.bin / name,
            "#!/bin/sh\n"
            "set -eu\n"
            + f"printf '{name} %s\\n' \"${{*-}}\" >> \"$CM_TEST_LOG\"\n"
            + textwrap.dedent(body).lstrip(),
        )

    def _write_stubs(self) -> None:
        self._write_stub(
            "id",
            r"""
            if [ "$#" -ne 2 ] || [ "$2" != "me" ]; then
                echo "unsupported id args: $*" >&2
                exit 64
            fi
            case "$1" in
                -u)
                    entry="$(grep -lxF me "$CM_TEST_STATE/users_by_uid"/*)" || {
                        echo "id: no users_by_uid entry for me" >&2
                        exit 70
                    }
                    if [ "$(printf '%s\n' "$entry" | wc -l)" -ne 1 ]; then
                        echo "id: multiple users_by_uid entries for me: $entry" >&2
                        exit 70
                    fi
                    basename "$entry"
                    ;;
                -g) cat "$CM_TEST_STATE/gid" ;;
                -gn) cat "$CM_TEST_STATE/groups_by_gid/$(cat "$CM_TEST_STATE/gid")" ;;
                *)
                    echo "unsupported id args: $*" >&2
                    exit 64
                    ;;
            esac
            """,
        )
        self._write_stub(
            "getent",
            r"""
            if [ "$#" -ne 2 ]; then
                echo "unsupported getent args: $*" >&2
                exit 64
            fi
            if [ "$1" = "group" ]; then
                entry="$CM_TEST_STATE/groups_by_gid/$2"
                [ -f "$entry" ] || exit 2
                printf '%s:x:%s:\n' "$(cat "$entry")" "$2"
                exit 0
            fi
            if [ "$1" = "passwd" ]; then
                entry="$CM_TEST_STATE/users_by_uid/$2"
                [ -f "$entry" ] || exit 2
                # gid comes from state, i.e. user me's primary gid: exact
                # for "me", an approximation for other seeded users
                # (uid-collision tests) -- acceptable for a test double.
                # Home and shell mirror the image's useradd flags.
                printf '%s:x:%s:%s::/home/%s:/bin/bash\n' \
                    "$(cat "$entry")" "$2" "$(cat "$CM_TEST_STATE/gid")" "$(cat "$entry")"
                exit 0
            fi
            echo "unsupported getent database: $1" >&2
            exit 64
            """,
        )
        self._write_stub(
            "groupmod",
            r"""
            if [ "$#" -ne 3 ] || [ "$1" != "-g" ] || [ "$3" != "me" ]; then
                echo "unsupported groupmod args: $*" >&2
                exit 64
            fi
            gid="$2"
            entry="$CM_TEST_STATE/groups_by_gid/$gid"
            if [ -f "$entry" ] && [ "$(cat "$entry")" != "me" ]; then
                echo "groupmod: GID $gid already exists" >&2
                exit 4
            fi
            # count matches by line, not by word: tempdir paths may contain
            # spaces (e.g. a TMPDIR like "/tmp/cm space test")
            matches="$(grep -lxF me "$CM_TEST_STATE/groups_by_gid"/* 2>/dev/null || true)"
            count="$(printf '%s' "$matches" | grep -c '^' || true)"
            if [ "$count" -ne 1 ]; then
                echo "groupmod: expected exactly one 'me' group, got $count" >&2
                exit 4
            fi
            rm -f "$matches"
            printf 'me\n' > "$entry"
            # like real groupmod, passwd entries follow the group's gid change
            printf '%s\n' "$gid" > "$CM_TEST_STATE/gid"
            """,
        )
        self._write_stub(
            "usermod",
            r"""
            if [ "$#" -eq 3 ] && [ "$1" = "-g" ] && [ "$3" = "me" ]; then
                group="$2"
                # count matches by line, not by word: tempdir paths may
                # contain spaces (e.g. a TMPDIR like "/tmp/cm space test")
                matches="$(grep -lxF "$group" "$CM_TEST_STATE/groups_by_gid"/* 2>/dev/null || true)"
                count="$(printf '%s' "$matches" | grep -c '^' || true)"
                if [ "$count" -eq 0 ]; then
                    echo "usermod: group $group does not exist" >&2
                    exit 6
                fi
                if [ "$count" -ne 1 ]; then
                    echo "usermod: harness state corrupt: $count groups named $group" >&2
                    exit 4
                fi
                basename "$matches" > "$CM_TEST_STATE/gid"
                exit 0
            fi
            if [ "$#" -eq 3 ] && [ "$1" = "-u" ] && [ "$3" = "me" ]; then
                entry="$CM_TEST_STATE/users_by_uid/$2"
                if [ -f "$entry" ] && [ "$(cat "$entry")" != "me" ]; then
                    echo "usermod: UID $2 already exists" >&2
                    exit 4
                fi
                old="$(grep -lxF me "$CM_TEST_STATE/users_by_uid"/*)" || {
                    echo "usermod: no users_by_uid entry for me" >&2
                    exit 70
                }
                if [ "$(printf '%s\n' "$old" | wc -l)" -ne 1 ]; then
                    echo "usermod: multiple users_by_uid entries for me: $old" >&2
                    exit 70
                fi
                rm -f "$old"
                printf 'me\n' > "$entry"
                exit 0
            fi
            echo "unsupported usermod args: $*" >&2
            exit 64
            """,
        )
        self._write_stub("chown", "")
        self._write_stub(
            "install",
            r"""
            if [ "$#" -eq 8 ] && [ "$1" = "-d" ]; then
                # install -d -o OWNER -g GROUP -m MODE DIR
                mkdir -p "$8"
                chmod "$7" "$8"
            elif [ "$#" -eq 8 ] && [ "$1" = "-o" ]; then
                # install -o OWNER -g GROUP -m MODE SRC DST
                cp "$7" "$8"
                chmod "$6" "$8"
            else
                echo "unsupported install args: $*" >&2
                exit 64
            fi
            """,
        )
        self._write_stub(
            "sudo",
            r"""
            # Fail closed: only the exact probe entrypoint.sh issues is allowed,
            # and it runs the real test(1) so the writability check stays real.
            if [ "$#" -eq 5 ] && \
                [ "$1" = "-u" ] && \
                [ "$2" = "me" ] && \
                [ "$3" = "test" ] && \
                [ "$4" = "-w" ]; then
                exec test -w "$5"
            fi
            echo "unsupported sudo args: $*" >&2
            exit 64
            """,
        )
        self._write_stub("sshd", "")


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

    def assert_nothing_provisioned(self, harness: EntrypointHarness) -> None:
        log = harness.log_text()
        self.assertNotIn("install -o", log)
        self.assertNotIn("sshd -D -e", log)
        self.assertFalse(harness.authorized_keys_dst.exists())

    def test_entrypoint_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "entrypoint.sh")],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_relocated_entrypoint_has_no_stray_absolute_paths(self):
        harness = self.make_entrypoint_harness()
        relocated = harness.entrypoint.read_text(encoding="utf-8")

        harness._assert_relocation_complete(relocated)

        with self.assertRaises(AssertionError) as ctx:
            harness._assert_relocation_complete(relocated + "mkdir -p /run/sshd\n")
        self.assertIn("/run/sshd", str(ctx.exception))

    def test_entrypoint_default_matches_cm_authorized_keys_mount(self):
        script = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        expected = (
            f'AUTHORIZED_KEYS_SRC="${{{authorized_keys_src_env()}'
            f':-{authorized_keys_mount()}}}"'
        )

        self.assertIn(
            expected,
            script,
            "cm.py is the runtime source of truth for the authorized_keys "
            f"mount path: it passes {authorized_keys_src_env()}="
            f"{authorized_keys_mount()} to new containers, and entrypoint.sh's "
            "fallback (for manual `docker run`) must equal cm.py's "
            "AUTHORIZED_KEYS_MOUNT. Change cm.py and entrypoint.sh together, "
            "and keep README.md's SSH Keys section in step.",
        )

    def test_entrypoint_reads_authorized_keys_path_from_environment(self):
        harness = self.make_entrypoint_harness()
        missing = harness.temp_path / "missing_authorized_keys"

        result = harness.run(**{authorized_keys_src_env(): str(missing)})

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            f"authorized_keys source not found at {missing}", result.stderr
        )
        self.assert_nothing_provisioned(harness)

    def test_entrypoint_remaps_user_installs_authorized_keys_and_starts_sshd(self):
        harness = self.make_entrypoint_harness()
        bashrc = harness.home / ".bashrc"
        bashrc.write_text("# keep me\n", encoding="utf-8")

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.uid(), "1234")
        self.assertEqual(harness.state_value("gid"), "2345")
        self.assertEqual(harness.group_name(), "me")
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
            f"chown me:me {harness.home}\n",
            f"chown me:me {harness.home}/.bashrc\n",
            f"install -d -o me -g me -m 700 {harness.ssh_dir}",
            harness.install_keys_log_line(),
            harness.workspace_probe_log_line(),
            "sshd -D -e",
        )

    def test_entrypoint_recursively_chowns_pre_existing_ssh_dir(self):
        harness = self.make_entrypoint_harness()
        harness.ssh_dir.mkdir()
        old_key = harness.ssh_dir / "id_ed25519"
        old_key.write_text("old private key\n", encoding="utf-8")

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old_key.read_text(encoding="utf-8"), "old private key\n")
        self.assertEqual(
            harness.authorized_keys_dst.read_text(encoding="utf-8"),
            harness.authorized_keys_src.read_text(encoding="utf-8"),
        )

        log = harness.log_text()
        self.assert_log_order(
            log,
            f"chown me:me {harness.home}",
            f"chown -R me:me {harness.ssh_dir}",
            f"install -d -o me -g me -m 700 {harness.ssh_dir}",
        )

    def test_entrypoint_without_host_uid_gid_skips_remap_and_starts_sshd(self):
        harness = self.make_entrypoint_harness()

        result = harness.run()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.uid(), "1000")
        self.assertEqual(harness.state_value("gid"), "1000")
        self.assertEqual(harness.group_name(), "me")
        self.assertEqual(
            (harness.root / "etc" / "profile.d" / "cm-path.sh").read_text(
                encoding="utf-8"
            ),
            'export PATH="$HOME/.local/bin:$PATH"\n',
        )
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
            f"install -d -o me -g me -m 700 {harness.ssh_dir}",
            (
                "install -o me -g me -m 600 "
                f"{harness.authorized_keys_src} {harness.authorized_keys_dst}"
            ),
            "sshd -D -e",
        )
        self.assertNotIn("groupmod", log)
        self.assertNotIn("usermod", log)
        self.assertNotIn("chown", log)
        self.assertNotIn("sudo", log)

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

        result = harness.run(CM_HOST_UID="1000", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(harness.uid(), "1000")
        self.assertEqual(harness.state_value("gid"), "2345")
        self.assertEqual(harness.group_name(), "hostgroup")

        log = harness.log_text()
        self.assertIn("getent group 2345", log)
        self.assertIn("usermod -g hostgroup me", log)
        self.assertNotIn("groupmod -g 2345 me", log)
        self.assertIn(f"install -d -o me -g hostgroup -m 700 {harness.ssh_dir}", log)

    def test_entrypoint_rejects_uid_collision_before_usermod(self):
        harness = self.make_entrypoint_harness()
        harness.add_user("taken", "1234")

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="1000")

        self.assertEqual(result.returncode, 1)
        self.assertIn("UID is already used by taken", result.stderr)

        log = harness.log_text()
        self.assertIn("getent passwd 1234", log)
        self.assertNotIn("usermod -u 1234 me", log)
        self.assert_nothing_provisioned(harness)

    def test_entrypoint_rejects_invalid_uid_gid_before_remapping(self):
        harness = self.make_entrypoint_harness()

        result = harness.run(CM_HOST_UID="0", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 1)
        self.assertIn("must be positive integers", result.stderr)

        log = harness.log_text()
        self.assertNotIn("groupmod", log)
        self.assertNotIn("usermod", log)
        self.assert_nothing_provisioned(harness)

    def test_entrypoint_rejects_missing_authorized_keys_source(self):
        harness = self.make_entrypoint_harness()
        harness.authorized_keys_src.unlink()

        result = harness.run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("authorized_keys source not found at", result.stderr)
        self.assert_nothing_provisioned(harness)

    def test_entrypoint_rejects_empty_authorized_keys_source(self):
        harness = self.make_entrypoint_harness()
        harness.authorized_keys_src.write_text("", encoding="utf-8")

        result = harness.run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("authorized_keys source is empty at", result.stderr)
        self.assert_nothing_provisioned(harness)

    @unittest.skipIf(
        os.geteuid() == 0,
        "root bypasses permission bits; 0o555 cannot model an unwritable workspace",
    )
    def test_entrypoint_rejects_unwritable_workspace_before_sshd(self):
        harness = self.make_entrypoint_harness()
        harness.workspace.chmod(0o555)

        result = harness.run(CM_HOST_UID="1234", CM_HOST_GID="2345")

        self.assertEqual(result.returncode, 1)
        self.assertIn("workspace is not writable by user me", result.stderr)
        self.assertTrue(harness.authorized_keys_dst.exists())

        log = harness.log_text()
        self.assert_log_order(
            log,
            harness.install_keys_log_line(),
            harness.workspace_probe_log_line(),
        )
        self.assertNotIn("sshd -D -e", log)

    def test_dockerfile_runtime_contract_order(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertLess(
            dockerfile.index("COPY --chmod=755 entrypoint.sh"),
            dockerfile.index("ENTRYPOINT"),
        )
        self.assertLess(dockerfile.index("HEALTHCHECK"), dockerfile.index("ENTRYPOINT"))
