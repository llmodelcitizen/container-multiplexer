from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("CM_REAL_DOCKER_SMOKE") == "1",
    "set CM_REAL_DOCKER_SMOKE=1 to run the opt-in Docker smoke test",
)
class RealDockerSmokeTests(unittest.TestCase):
    def test_builds_base_and_runtime_images_with_real_docker(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
