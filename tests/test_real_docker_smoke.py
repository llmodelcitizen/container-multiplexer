from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from tests.support import FAKE_SSH_KEY, authorized_keys_mount


ROOT = Path(__file__).resolve().parents[1]
SMOKE_UID = "42424"
SMOKE_GID = "42425"
READINESS_TIMEOUT_SECONDS = 60.0
READINESS_POLL_SECONDS = 0.5
# One stdout line per command in the probe's `sh -ec` script below. Keep in
# sync with the probe when adding or removing commands.
EXPECTED_PROBE_LINES = 4


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

    def container_logs(self, container_name: str) -> str:
        logs = self.run_docker(
            ["logs", container_name],
            check=False,
            capture_output=True,
        )
        return logs.stdout + logs.stderr

    def wait_for_container_ready(self, container_name: str) -> None:
        """Wait for the entrypoint to finish provisioning before probing.

        The entrypoint only execs sshd after its groupmod/usermod/install
        steps complete, so a listening port 22 (the HEALTHCHECK condition)
        means provisioning is done. Poll it on a tight cadence instead of
        waiting out the healthcheck's 45s start-period and 30s interval.
        """
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while True:
            running = self.run_docker(
                ["inspect", "--format", "{{.State.Running}}", container_name],
                check=False,
                capture_output=True,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                self.fail(
                    "container exited before becoming ready\ncontainer logs:\n"
                    + self.container_logs(container_name)
                )
            ready = self.run_docker(
                ["exec", container_name, "nc", "-z", "127.0.0.1", "22"],
                check=False,
                capture_output=True,
            )
            if ready.returncode == 0:
                return
            if time.monotonic() >= deadline:
                self.fail(
                    "container not ready within "
                    f"{READINESS_TIMEOUT_SECONDS:.0f}s\ncontainer logs:\n"
                    + self.container_logs(container_name)
                )
            time.sleep(READINESS_POLL_SECONDS)

    def test_builds_and_runs_runtime_image_with_real_docker(self) -> None:
        self.run_docker(["version"], capture_output=True)
        self.run_docker(["build", "-t", "cm-base:latest", "-f", "Dockerfile.base", "."])
        self.run_docker(["build", "-t", "cm:smoke", "."])

        with tempfile.TemporaryDirectory() as temp_dir:
            auth_keys = Path(temp_dir) / "authorized_keys"
            auth_keys.write_text(FAKE_SSH_KEY, encoding="utf-8")
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
                        f"{auth_keys}:{authorized_keys_mount()}:ro",
                        "cm:smoke",
                    ],
                    capture_output=True,
                )
                self.wait_for_container_ready(container_name)
                probe = self.run_docker(
                    [
                        "exec",
                        container_name,
                        "sh",
                        "-ec",
                        "id -u me; id -g me; "
                        "stat -c '%u:%g %a %s' /home/me/.ssh/authorized_keys; "
                        "cat /home/me/.ssh/authorized_keys",
                    ],
                    check=False,
                    capture_output=True,
                )
                failure = (
                    f"probe failed: stdout={probe.stdout!r} "
                    f"stderr={probe.stderr!r}"
                )
                if probe.returncode != 0:
                    failure += (
                        "\ncontainer logs:\n"
                        f"{self.container_logs(container_name)}"
                    )
                self.assertEqual(probe.returncode, 0, msg=failure)

                lines = probe.stdout.strip().splitlines()
                self.assertEqual(len(lines), EXPECTED_PROBE_LINES, msg=probe.stdout)
                uid = lines[0]
                gid = lines[1]
                authorized_keys = lines[2]
                key_line = lines[3]

                self.assertEqual(uid, SMOKE_UID)
                self.assertEqual(gid, SMOKE_GID)
                self.assertTrue(
                    authorized_keys.startswith(f"{SMOKE_UID}:{SMOKE_GID} 600 "),
                    authorized_keys,
                )
                self.assertEqual(key_line, "ssh-ed25519 fake")
            finally:
                self.run_docker(
                    ["rm", "-f", container_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
