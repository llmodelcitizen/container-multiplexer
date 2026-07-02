from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeImageTests(unittest.TestCase):
    def test_entrypoint_is_valid_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(ROOT / "entrypoint.sh")],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entrypoint_execs_sshd_in_foreground(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertIn("exec /usr/sbin/sshd -D -e", entrypoint)
        self.assertNotIn("tail -f /dev/null", entrypoint)

    def test_entrypoint_installs_path_profile_without_mutating_bashrc(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertIn("cat > /etc/profile.d/cm-path.sh", entrypoint)
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', entrypoint)
        self.assertNotIn(">> /home/me/.bashrc", entrypoint)

    def test_dockerfile_has_ssh_healthcheck(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("nc -z 127.0.0.1 22", dockerfile)

    def test_base_image_sets_default_me_uid_gid(self):
        dockerfile = (ROOT / "Dockerfile.base").read_text()

        self.assertIn("groupadd -g 1000 me", dockerfile)
        self.assertIn("useradd -m -u 1000 -g 1000 -s /bin/bash me", dockerfile)

    def test_entrypoint_remaps_me_and_checks_workspace_writable(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertIn("CM_HOST_UID", entrypoint)
        self.assertIn("CM_HOST_GID", entrypoint)
        self.assertIn("usermod -u", entrypoint)
        self.assertIn('sudo -u me test -w "$WORKSPACE_DIR"', entrypoint)

    def test_entrypoint_validates_uid_gid_before_remapping(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertLess(
            entrypoint.index('[[ "${CM_HOST_UID:-}" =~ ^[1-9][0-9]*$ ]]'),
            entrypoint.index('current_uid="$(id -u me)"'),
        )
        self.assertLess(entrypoint.index("groupmod -g"), entrypoint.index("usermod -u"))
        self.assertLess(entrypoint.index("usermod -u"), entrypoint.index('chown me:"$(id -gn me)" /home/me'))

    def test_entrypoint_handles_gid_collision_before_groupmod(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertLess(
            entrypoint.index('target_group="$(getent group "$CM_HOST_GID"'),
            entrypoint.index('groupmod -g "$CM_HOST_GID" me'),
        )
        self.assertIn('usermod -g "$target_group" me', entrypoint)

    def test_entrypoint_rejects_uid_collision_and_installs_authorized_keys(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertLess(
            entrypoint.index('target_user="$(getent passwd "$CM_HOST_UID"'),
            entrypoint.index('usermod -u "$CM_HOST_UID" me'),
        )
        self.assertIn("UID is already used by $target_user", entrypoint)
        self.assertLess(
            entrypoint.index('install -d -o me -g "$ME_GROUP" -m 700 "$SSH_DIR"'),
            entrypoint.index('install -o me -g "$ME_GROUP" -m 600 "$AUTHORIZED_KEYS_SRC" "$AUTHORIZED_KEYS_DST"'),
        )

    def test_dockerfile_runtime_contract_order(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertLess(dockerfile.index("COPY --chmod=755 entrypoint.sh"), dockerfile.index("ENTRYPOINT"))
        self.assertLess(dockerfile.index("HEALTHCHECK"), dockerfile.index("ENTRYPOINT"))


if __name__ == "__main__":
    unittest.main()
