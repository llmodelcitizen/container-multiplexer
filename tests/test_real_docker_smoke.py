from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from tests.support import load_cm


ROOT = Path(__file__).resolve().parents[1]
AUTHORIZED_KEYS_MOUNT = load_cm().AUTHORIZED_KEYS_MOUNT
SMOKE_UID = "42424"
SMOKE_GID = "42425"


@unittest.skipUnless(
    os.environ.get("CM_REAL_DOCKER_SMOKE") == "1",
    "set CM_REAL_DOCKER_SMOKE=1 to run the opt-in Docker smoke test",
)
class RealDockerSmokeTests(unittest.TestCase):
    def run_docker(
        self, args: list[str], *, check: bool = True, **kwargs
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            cwd=ROOT,
            text=True,
            check=check,
            **kwargs,
        )

    def test_builds_and_runs_runtime_image_with_real_docker(self) -> None:
        self.run_docker(["version"], capture_output=True)
        self.run_docker(["build", "-t", "cm-base:latest", "-f", "Dockerfile.base", "."])
        self.run_docker(["build", "-t", "cm:smoke", "."])

        with tempfile.TemporaryDirectory() as temp_dir:
            auth_keys = Path(temp_dir) / "authorized_keys"
            auth_keys.write_text("ssh-ed25519 fake\n", encoding="utf-8")
            container_name = f"cm-smoke-{uuid.uuid4().hex}"

            try:
                self.run_docker(
                    [
                        "run",
                        "--name",
                        container_name,
                        "-d",
                        "-e",
                        f"CM_HOST_UID={SMOKE_UID}",
                        "-e",
                        f"CM_HOST_GID={SMOKE_GID}",
                        "-v",
                        f"{auth_keys}:{AUTHORIZED_KEYS_MOUNT}:ro",
                        "cm:smoke",
                    ],
                    capture_output=True,
                )
                probe = self.run_docker(
                    [
                        "exec",
                        container_name,
                        "sh",
                        "-ec",
                        "id -u me; id -g me; "
                        "stat -c '%u:%g %a %s' /home/me/.ssh/authorized_keys",
                    ],
                    capture_output=True,
                )
                uid, gid, authorized_keys = probe.stdout.strip().splitlines()

                self.assertEqual(uid, SMOKE_UID)
                self.assertEqual(gid, SMOKE_GID)
                self.assertTrue(
                    authorized_keys.startswith(f"{SMOKE_UID}:{SMOKE_GID} 600 "),
                    authorized_keys,
                )
            finally:
                self.run_docker(
                    ["rm", "-f", container_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


if __name__ == "__main__":
    unittest.main()
