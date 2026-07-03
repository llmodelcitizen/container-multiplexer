from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SMOKE_UID = "42424"
SMOKE_GID = "42424"


@unittest.skipUnless(
    os.environ.get("CM_REAL_DOCKER_SMOKE") == "1",
    "set CM_REAL_DOCKER_SMOKE=1 to run the opt-in Docker smoke test",
)
class RealDockerSmokeTests(unittest.TestCase):
    def run_docker(self, args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            cwd=ROOT,
            text=True,
            check=True,
            **kwargs,
        )

    def test_builds_and_runs_runtime_image_with_real_docker(self) -> None:
        subprocess.run(
            ["docker", "version"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["docker", "build", "-t", "cm-base:latest", "-f", "Dockerfile.base", "."],
            cwd=ROOT,
            text=True,
            check=True,
        )
        subprocess.run(
            ["docker", "build", "-t", "cm:smoke", "."],
            cwd=ROOT,
            text=True,
            check=True,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            auth_keys = Path(temp_dir) / "authorized_keys"
            auth_keys.write_text(
                "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAICmSmokeKey cm-smoke\n",
                encoding="utf-8",
            )
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
                        f"{auth_keys}:/tmp/cm_authorized_keys:ro",
                        "cm:smoke",
                    ],
                    capture_output=True,
                )
                uid = self.run_docker(
                    ["exec", container_name, "id", "-u", "me"],
                    capture_output=True,
                ).stdout.strip()
                gid = self.run_docker(
                    ["exec", container_name, "id", "-g", "me"],
                    capture_output=True,
                ).stdout.strip()
                authorized_keys = self.run_docker(
                    [
                        "exec",
                        container_name,
                        "stat",
                        "-c",
                        "%u:%g %a %s",
                        "/home/me/.ssh/authorized_keys",
                    ],
                    capture_output=True,
                ).stdout.strip()

                self.assertEqual(uid, SMOKE_UID)
                self.assertEqual(gid, SMOKE_GID)
                self.assertTrue(
                    authorized_keys.startswith(f"{SMOKE_UID}:{SMOKE_GID} 600 "),
                    authorized_keys,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
