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


if __name__ == "__main__":
    unittest.main()
