from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeImageTests(unittest.TestCase):
    def test_entrypoint_execs_sshd_in_foreground(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertIn("exec /usr/sbin/sshd -D -e", entrypoint)
        self.assertNotIn("tail -f /dev/null", entrypoint)

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


if __name__ == "__main__":
    unittest.main()
