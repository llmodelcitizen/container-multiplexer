#!/usr/bin/env python3
"""CM - Multi-instance Docker environment manager."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

import shutil
import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

if TYPE_CHECKING:
    import docker

_DOCKER_IMPORT_ERROR = (
    "Error: Python 'docker' package not installed. See README for setup instructions."
)
_docker_sdk: Any | None = None

SCRIPT_DIR = Path(__file__).parent.resolve()
CM_HOME = Path.home() / ".cm"
BASE_PORT = 2200
SSH_CONTAINER_PORT = 22
SSH_CONTAINER_PORT_PROTO = f"{SSH_CONTAINER_PORT}/tcp"
MAX_INSTANCE = 499
VERSION = "dev"
IMAGE_NAME = "cm"
WORKSPACES_DIR = CM_HOME / "workspaces"
AUTHORIZED_KEYS_PATH = CM_HOME / "authorized_keys"
AUTHORIZED_KEYS_MOUNT = "/tmp/cm_authorized_keys"
CONTAINER_DEFAULT_UID = 1000
CONTAINER_DEFAULT_GID = 1000
LINUX_HOST_UID_ENV = "CM_HOST_UID"
LINUX_HOST_GID_ENV = "CM_HOST_GID"


class WorkspacePreflightError(RuntimeError):
    """Raised when the host workspace would not be usable in the container."""


MANAGED_LABEL = "cm.managed"
MANAGED_LABEL_VALUE = "true"


class UnmanagedContainerNameError(RuntimeError):
    """Raised when a cm-NNN Docker name is owned by another container."""

    def __init__(self, name: str):
        super().__init__(
            f"Error: Docker name '{name}' is occupied by an unmanaged container "
            f"(missing label {MANAGED_LABEL}={MANAGED_LABEL_VALUE})."
        )


def get_cm_command_path() -> Path:
    """Return the command path nested cm commands should use."""
    wrapper_path = SCRIPT_DIR / "cm"
    if wrapper_path.exists():
        return wrapper_path
    return SCRIPT_DIR / "cm.py"


def get_docker_sdk() -> Any:
    """Load the Docker SDK only when a Docker-backed command needs it."""
    global _docker_sdk

    if _docker_sdk is None:
        try:
            import docker as docker_sdk
        except ImportError:
            sys.exit(_DOCKER_IMPORT_ERROR)
        _docker_sdk = docker_sdk

    return _docker_sdk


def run_tmux(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a tmux command, printing it to stderr first."""
    cmd = ["tmux"] + args
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        sys.exit("Error: tmux is not installed.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Error: tmux command failed: {' '.join(args)}")


def exec_tmux(args: list[str]) -> None:
    """Replace current process with tmux command, printing it to stderr first."""
    cmd = ["tmux"] + args
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    try:
        os.execlp("tmux", *cmd)
    except FileNotFoundError:
        sys.exit("Error: tmux is not installed.")


def get_client() -> docker.DockerClient:
    """Get Docker client."""
    docker_sdk = get_docker_sdk()
    try:
        return docker_sdk.from_env()
    except docker_sdk.errors.DockerException:
        sys.exit("Error: Cannot connect to Docker. Is the Docker daemon running?")


def get_instance_config(n: int) -> dict:
    """Get configuration for instance N."""
    return {
        "container": f"cm-{n:03d}",
        "port": BASE_PORT + n,
        "workspace": WORKSPACES_DIR / f"cm.{n:03d}",
    }


def get_container(client: docker.DockerClient, name: str):
    """Get a container by name, or None if not found."""
    docker_sdk = get_docker_sdk()
    try:
        return client.containers.get(name)
    except docker_sdk.errors.NotFound:
        return None


def get_container_labels(container) -> dict:
    """Get Docker labels from a high-level container object."""
    labels = getattr(container, "labels", None)
    if isinstance(labels, dict):
        return labels

    attrs = getattr(container, "attrs", {})
    if not isinstance(attrs, dict):
        return {}

    config = attrs.get("Config", {})
    if not isinstance(config, dict):
        return {}

    labels = config.get("Labels", {})
    if isinstance(labels, dict):
        return labels
    return {}


def is_managed_container(container) -> bool:
    """Return True when a container carries the cm managed label."""
    return get_container_labels(container).get(MANAGED_LABEL) == MANAGED_LABEL_VALUE


def get_managed_container(client: docker.DockerClient, name: str):
    """Get a cm-managed container by name, refusing unmanaged name collisions."""
    container = get_container(client, name)
    if container is None:
        return None
    if not is_managed_container(container):
        raise UnmanagedContainerNameError(name)
    return container


def get_unmanaged_container_error(client: docker.DockerClient, name: str) -> str | None:
    """Return a clear error if name is occupied by an unmanaged container."""
    docker_sdk = get_docker_sdk()
    try:
        get_managed_container(client, name)
    except UnmanagedContainerNameError as e:
        return str(e)
    except docker_sdk.errors.APIError:
        return None
    return None


def is_native_linux_host() -> bool:
    """Return True when cm is running directly on a Linux host."""
    return sys.platform.startswith("linux")


def get_linux_host_identity() -> tuple[int, int] | None:
    """Return the native Linux host UID/GID to mirror into the container."""
    if not is_native_linux_host():
        return None
    if not all(hasattr(os, name) for name in ("getuid", "geteuid", "getgid")):
        return None

    uid = os.getuid()
    euid = os.geteuid()
    gid = os.getgid()
    if uid == 0 or euid == 0:
        raise WorkspacePreflightError(
            "Error: do not run cm start/restart with sudo or as root on native Linux.\n"
            "Running as root can create root-owned workspaces that user me cannot write.\n"
            "Run cm as your normal user after granting Docker access to that user."
        )

    return (uid, gid)


def get_container_environment() -> dict[str, str] | None:
    """Return environment variables to apply when creating new containers."""
    identity = get_linux_host_identity()
    if identity is None:
        return None

    uid, gid = identity
    return {
        LINUX_HOST_UID_ENV: str(uid),
        LINUX_HOST_GID_ENV: str(gid),
    }


def prepare_workspace(workspace: Path) -> None:
    """Create and verify a workspace before it is bind-mounted."""
    get_linux_host_identity()

    try:
        workspace.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise WorkspacePreflightError(
            f"Error: cannot create workspace directory {workspace}: {e}"
        ) from e

    if not is_native_linux_host():
        return

    try:
        with tempfile.NamedTemporaryFile(
            dir=workspace, prefix=".cm-write-test-", delete=True
        ):
            pass
    except OSError as e:
        raise WorkspacePreflightError(
            f"Error: workspace is not writable by the current host user: {workspace}\n"
            "Fix the ownership or permissions before starting the instance."
        ) from e


def get_container_env(container) -> dict[str, str]:
    """Extract environment variables from Docker inspect attrs."""
    attrs = getattr(container, "attrs", {})
    if not isinstance(attrs, dict):
        return {}

    config = attrs.get("Config", {})
    if not isinstance(config, dict):
        return {}

    env_items = config.get("Env", [])
    if not isinstance(env_items, list):
        return {}

    env = {}
    for item in env_items:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            env[key] = value
    return env


def get_existing_container_identity_error(container, n: int) -> str | None:
    """Return an error when an existing container cannot match this host UID/GID."""
    identity = get_linux_host_identity()
    if identity is None:
        return None

    uid, gid = identity
    env = get_container_env(container)
    container_uid = env.get(LINUX_HOST_UID_ENV)
    container_gid = env.get(LINUX_HOST_GID_ENV)

    if container_uid == str(uid) and container_gid == str(gid):
        return None
    if container_uid is None and container_gid is None and uid == CONTAINER_DEFAULT_UID:
        return None

    configured = (
        f"{container_uid or str(CONTAINER_DEFAULT_UID)}:"
        f"{container_gid or str(CONTAINER_DEFAULT_GID)}"
    )
    return (
        f"Instance {n} exists but was created for Linux UID/GID {configured}, "
        f"not the current host UID/GID {uid}:{gid}. Stop and remove the container, "
        f"then run 'cm start {n}' again; the workspace directory remains."
    )


def _parse_port(port: object) -> int | None:
    """Parse a Docker port value into an int."""
    if port is None:
        return None
    try:
        parsed = int(str(port))
    except (TypeError, ValueError):
        return None
    if parsed < 1 or parsed > 65535:
        return None
    return parsed


def get_ssh_port_from_summary(container_summary: dict) -> int | None:
    """Get the published SSH port from a Docker container summary."""
    ports = container_summary.get("Ports", [])
    if not isinstance(ports, list):
        return None

    for port_info in ports:
        if not isinstance(port_info, dict):
            continue
        if _parse_port(port_info.get("PrivatePort")) != SSH_CONTAINER_PORT:
            continue
        if port_info.get("Type", "tcp") != "tcp":
            continue
        public_port = _parse_port(port_info.get("PublicPort"))
        if public_port is not None:
            return public_port

    return None


def _get_ssh_port_from_bindings(port_bindings: object) -> int | None:
    """Get the published SSH port from Docker inspect-style port bindings."""
    if not isinstance(port_bindings, dict):
        return None

    bindings = port_bindings.get(SSH_CONTAINER_PORT_PROTO)
    if isinstance(bindings, dict):
        bindings = [bindings]
    if not isinstance(bindings, list):
        return None

    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        host_port = _parse_port(binding.get("HostPort"))
        if host_port is not None:
            return host_port

    return None


def get_ssh_port_from_attrs(attrs: object) -> int | None:
    """Get the published SSH port from Docker inspect attrs."""
    if not isinstance(attrs, dict):
        return None

    network_settings = attrs.get("NetworkSettings", {})
    if isinstance(network_settings, dict):
        port = _get_ssh_port_from_bindings(network_settings.get("Ports", {}))
        if port is not None:
            return port

    host_config = attrs.get("HostConfig", {})
    if isinstance(host_config, dict):
        return _get_ssh_port_from_bindings(host_config.get("PortBindings", {}))

    return None


def get_container_ssh_port(container, fallback_port: int | None = None) -> int | None:
    """Get a container's published SSH port, falling back when unavailable."""
    port = get_ssh_port_from_attrs(getattr(container, "attrs", None))
    return fallback_port if port is None else port


def get_list_ssh_port(client: docker.DockerClient, container_summary: dict,
                      fallback_port: int | None = None) -> int | None:
    """Get the SSH port for list output from summary, inspect, or fallback."""
    port = get_ssh_port_from_summary(container_summary)
    if port is not None:
        return port

    names = container_summary.get("Names", [])
    if not names:
        return fallback_port

    name = names[0].lstrip("/")
    docker_sdk = get_docker_sdk()
    try:
        container = get_container(client, name)
    except docker_sdk.errors.APIError:
        return fallback_port

    if not container:
        return fallback_port

    return get_container_ssh_port(container, fallback_port)


def is_port_allocation_error(error: Exception) -> bool:
    """Return True if Docker failed because the requested host port is busy."""
    message = str(error).lower()
    return (
        "port is already allocated" in message
        or "bind: address already in use" in message
        or (
            "ports are not available" in message
            and "address already in use" in message
        )
    )


def parse_instances(args: list[str]) -> list[int]:
    """Parse instance arguments into a list of instance numbers.

    Supports:
        - Single numbers: 1 2 3
        - Ranges: 1-5
        - Mixed: 1 3-5 7
        - 'all' keyword for stop command
    """
    instances = []
    for arg in args:
        if "-" in arg and arg != "all":
            try:
                start, end = arg.split("-")
                start, end = int(start), int(end)
                if start > end:
                    start, end = end, start
                instances.extend(range(start, end + 1))
            except ValueError:
                sys.exit(f"Invalid range: {arg}")
        else:
            try:
                instances.append(int(arg))
            except ValueError:
                sys.exit(f"Invalid instance: {arg}")

    # Validate instance range
    for n in instances:
        if n < 1:
            sys.exit(f"Instance {n} must be positive")
        if n > MAX_INSTANCE:
            sys.exit(f"Instance {n} exceeds maximum ({MAX_INSTANCE})")

    return sorted(set(instances))


def get_running_instances(client: docker.DockerClient) -> list[int]:
    """Get list of running instance numbers."""
    containers = client.api.containers(
        filters={"label": "cm.managed=true", "status": "running"}
    )
    instances = []
    for c in containers:
        names = c.get("Names", [])
        if not names:
            continue
        try:
            n = int(names[0].lstrip("/").split("-")[1])
            instances.append(n)
        except (IndexError, ValueError):
            continue
    return sorted(instances)


def get_dead_instances(client: docker.DockerClient) -> list[int]:
    """Get list of dead (exited/created) instance numbers."""
    containers = client.api.containers(all=True, filters={"label": "cm.managed=true"})
    instances = []
    for c in containers:
        state = c.get("State", "")
        if state == "running":
            continue
        names = c.get("Names", [])
        if not names:
            continue
        try:
            n = int(names[0].lstrip("/").split("-")[1])
            instances.append(n)
        except (IndexError, ValueError):
            continue
    return sorted(instances)


def get_next_session_name() -> tuple[str, bool]:
    """Get next available tmux session name.

    Returns (session_name, existed) where existed is True if a cm session
    already existed and we're creating a new one.
    """
    n = 1
    existed = False
    while True:
        name = f"cm-s{n}"
        result = run_tmux(["has-session", "-t", name],
                                capture_output=True)
        if result.returncode != 0:
            return name, existed
        existed = True
        n += 1


def get_authorized_keys_path() -> Path:
    """Return the authorized_keys file to stage into containers."""
    path = AUTHORIZED_KEYS_PATH

    if not path.is_file():
        print(f"Error: authorized_keys source is not a file: {path}", file=sys.stderr)
        print("Create the default cm SSH key, then rerun the installer:", file=sys.stderr)
        print("  mkdir -p ~/.ssh", file=sys.stderr)
        print("  chmod 700 ~/.ssh", file=sys.stderr)
        print("  ssh-keygen -t ed25519 -f ~/.ssh/cm_ed25519", file=sys.stderr)
        print("  chmod 400 ~/.ssh/cm_ed25519", file=sys.stderr)
        print("  ./install.sh", file=sys.stderr)
        sys.exit(1)
    try:
        if path.stat().st_size == 0:
            print(f"Error: authorized_keys source is empty: {path}", file=sys.stderr)
            sys.exit(1)
    except OSError as e:
        print(f"Error: Cannot read authorized_keys source {path}: {e}", file=sys.stderr)
        sys.exit(1)
    if not os.access(path, os.R_OK):
        print(f"Error: Cannot read authorized_keys source: {path}", file=sys.stderr)
        sys.exit(1)

    return path


def try_start_container(client: docker.DockerClient, n: int, cfg: dict) -> tuple[int | None, str | None]:
    """Try to start a container, retrying with next port if port is in use.

    Returns (port, None) on success, or (None, error_message) on failure.
    """
    port = cfg["port"]
    max_port_attempts = 100
    auth_keys = get_authorized_keys_path()
    environment = get_container_environment()
    docker_sdk = get_docker_sdk()

    for attempt in range(max_port_attempts):
        try:
            run_kwargs = {
                "detach": True,
                "name": cfg["container"],
                "hostname": cfg["container"],
                "ports": {"22/tcp": ("127.0.0.1", port)},
                "volumes": {
                    str(auth_keys): {
                        "bind": AUTHORIZED_KEYS_MOUNT,
                        "mode": "ro",
                    },
                    str(cfg["workspace"]): {
                        "bind": "/home/me/workspace",
                        "mode": "rw",
                    },
                },
                "restart_policy": {"Name": "unless-stopped"},
                "labels": {MANAGED_LABEL: MANAGED_LABEL_VALUE},
            }
            if environment:
                run_kwargs["environment"] = environment
            client.containers.run(IMAGE_NAME, **run_kwargs)
            return (port, None)
        except docker_sdk.errors.APIError as e:
            unmanaged_error = get_unmanaged_container_error(client, cfg["container"])
            if unmanaged_error:
                return (None, unmanaged_error)

            if is_port_allocation_error(e):
                # Remove the failed container before retrying with new port
                try:
                    container = get_managed_container(client, cfg["container"])
                except UnmanagedContainerNameError as unmanaged:
                    return (None, str(unmanaged))
                if container:
                    container.remove(force=True)
                port += 1
                continue
            return (None, str(e))

    return (None, f"Could not find available port after {max_port_attempts} attempts")


def start_instance(client: docker.DockerClient, n: int) -> bool:
    """Start a single instance. Returns True on success."""
    cfg = get_instance_config(n)
    docker_sdk = get_docker_sdk()
    try:
        prepare_workspace(cfg["workspace"])
    except WorkspacePreflightError as e:
        print(e, file=sys.stderr)
        return False

    # Check if container exists
    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return False

    if container:
        identity_error = get_existing_container_identity_error(container, n)
        if identity_error:
            print(identity_error)
            return False
        if container.status == "running":
            print(f"Instance {n} is already running")
            return True
        # Start existing stopped container
        container.start()
        print(f"Started instance {n} (existing container)")
        return True

    # Check if image exists
    try:
        client.images.get(IMAGE_NAME)
    except docker_sdk.errors.NotFound:
        print(f"Image '{IMAGE_NAME}' not found. Build it first:")
        print(f"  docker build -t {IMAGE_NAME} .")
        return False

    # Create and start new container
    port, error = try_start_container(client, n, cfg)
    if error:
        print(f"Failed to start instance {n}: {error}")
        return False

    if port != cfg["port"]:
        print(f"Started instance {n} (port {port} - {cfg['port']} was in use, workspace {cfg['workspace'].name}/)")
    else:
        print(f"Started instance {n} (port {port}, workspace {cfg['workspace'].name}/)")
    return True


def stop_instance(client: docker.DockerClient, n: int) -> bool:
    """Stop a single instance. Returns True on success."""
    cfg = get_instance_config(n)

    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return False
    if not container:
        print(f"Instance {n} does not exist")
        return False

    if container.status != "running":
        print(f"Instance {n} is not running")
        return False

    container.stop()
    print(f"Stopped instance {n}")
    return True


def restart_instance(client: docker.DockerClient, n: int) -> bool:
    """Restart a single instance. Returns True on success."""
    cfg = get_instance_config(n)
    try:
        prepare_workspace(cfg["workspace"])
    except WorkspacePreflightError as e:
        print(e, file=sys.stderr)
        return False

    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return False
    if container:
        identity_error = get_existing_container_identity_error(container, n)
        if identity_error:
            print(identity_error)
            return False
    if container and container.status == "running":
        container.stop()

    return start_instance(client, n)


def rm_instance(client: docker.DockerClient, n: int) -> bool:
    """Remove a dead (non-running) container. Returns True on success."""
    cfg = get_instance_config(n)

    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return False
    if not container:
        print(f"Instance {n} does not exist")
        return False

    if container.status == "running":
        print(f"Instance {n} is running (use 'stop' instead)")
        return False

    container.remove()
    print(f"Removed instance {n}")
    return True


def _start_instance_worker(n: int) -> tuple[int, bool, str]:
    """Worker function to start an instance in a thread."""
    client = get_client()
    docker_sdk = get_docker_sdk()
    try:
        cfg = get_instance_config(n)
        try:
            prepare_workspace(cfg["workspace"])
        except WorkspacePreflightError as e:
            return (n, False, str(e))

        try:
            container = get_managed_container(client, cfg["container"])
        except UnmanagedContainerNameError as e:
            return (n, False, str(e))

        if container:
            identity_error = get_existing_container_identity_error(container, n)
            if identity_error:
                return (n, False, identity_error)
            if container.status == "running":
                return (n, True, f"Instance {n} is already running")
            # Start existing stopped container
            container.start()
            return (n, True, f"Started instance {n} (existing container)")

        try:
            client.images.get(IMAGE_NAME)
        except docker_sdk.errors.NotFound:
            return (n, False, f"Image '{IMAGE_NAME}' not found")

        port, error = try_start_container(client, n, cfg)
        if error:
            return (n, False, f"Failed to start instance {n}: {error}")

        if port != cfg["port"]:
            return (n, True, f"Started instance {n} (port {port} - {cfg['port']} was in use, workspace {cfg['workspace'].name}/)")
        return (n, True, f"Started instance {n} (port {port}, workspace {cfg['workspace'].name}/)")
    finally:
        client.close()


def _stop_instance_worker(n: int) -> tuple[int, bool, str]:
    """Worker function to stop an instance in a thread."""
    client = get_client()
    try:
        cfg = get_instance_config(n)
        try:
            container = get_managed_container(client, cfg["container"])
        except UnmanagedContainerNameError as e:
            return (n, False, str(e))
        if not container:
            return (n, False, f"Instance {n} does not exist")
        if container.status != "running":
            return (n, False, f"Instance {n} is not running")
        container.stop()
        return (n, True, f"Stopped instance {n}")
    finally:
        client.close()


def _restart_instance_worker(n: int) -> tuple[int, bool, str]:
    """Worker function to restart an instance in a thread."""
    client = get_client()
    docker_sdk = get_docker_sdk()
    try:
        cfg = get_instance_config(n)
        try:
            prepare_workspace(cfg["workspace"])
        except WorkspacePreflightError as e:
            return (n, False, str(e))

        try:
            container = get_managed_container(client, cfg["container"])
        except UnmanagedContainerNameError as e:
            return (n, False, str(e))

        if container:
            identity_error = get_existing_container_identity_error(container, n)
            if identity_error:
                return (n, False, identity_error)
            if container.status == "running":
                container.stop()
            # Start existing container
            container.start()
            return (n, True, f"Restarted instance {n} (existing container)")

        try:
            client.images.get(IMAGE_NAME)
        except docker_sdk.errors.NotFound:
            return (n, False, f"Image '{IMAGE_NAME}' not found")

        port, error = try_start_container(client, n, cfg)
        if error:
            return (n, False, f"Failed to restart instance {n}: {error}")

        if port != cfg["port"]:
            return (n, True, f"Restarted instance {n} (port {port} - {cfg['port']} was in use, workspace {cfg['workspace'].name}/)")
        return (n, True, f"Restarted instance {n} (port {port}, workspace {cfg['workspace'].name}/)")
    finally:
        client.close()


def _rm_instance_worker(n: int) -> tuple[int, bool, str]:
    """Worker function to remove a dead container in a thread."""
    client = get_client()
    try:
        cfg = get_instance_config(n)
        try:
            container = get_managed_container(client, cfg["container"])
        except UnmanagedContainerNameError as e:
            return (n, False, str(e))
        if not container:
            return (n, False, f"Instance {n} does not exist")
        if container.status == "running":
            return (n, False, f"Instance {n} is running (use 'stop' instead)")
        container.remove()
        return (n, True, f"Removed instance {n}")
    finally:
        client.close()


def run_parallel(worker_func, instances: list[int]) -> bool:
    """Run worker function in parallel across instances.
    Returns True if all operations succeeded.
    """
    all_success = True
    with ThreadPoolExecutor(max_workers=min(len(instances), 32)) as executor:
        future_to_n = {executor.submit(worker_func, n): n for n in instances}
        for future in as_completed(future_to_n):
            try:
                _, success, message = future.result()
                print(message)
                if not success:
                    all_success = False
            except Exception as e:
                n = future_to_n[future]
                print(f"Instance {n}: unexpected error: {e}")
                all_success = False

    return all_success


def cmd_start(args: argparse.Namespace) -> int:
    """Start instances."""
    instances = parse_instances(args.instances)
    try:
        get_linux_host_identity()
    except WorkspacePreflightError as e:
        print(e, file=sys.stderr)
        return 1

    if not instances:
        print("No instances specified")
        return 1

    if len(instances) > 1:
        success = run_parallel(_start_instance_worker, instances)
    else:
        client = get_client()
        success = start_instance(client, instances[0])
    return 0 if success else 1


def cmd_restart(args: argparse.Namespace) -> int:
    """Restart instances."""
    try:
        get_linux_host_identity()
    except WorkspacePreflightError as e:
        print(e, file=sys.stderr)
        return 1

    if args.instances == ["all"]:
        client = get_client()
        instances = get_running_instances(client)
        if not instances:
            print("No running instances to restart")
            return 0
    else:
        instances = parse_instances(args.instances)

    if not instances:
        print("No instances specified")
        return 1

    if len(instances) > 1:
        success = run_parallel(_restart_instance_worker, instances)
    else:
        client = get_client()
        success = restart_instance(client, instances[0])
    return 0 if success else 1


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop instances."""
    if args.instances == ["all"]:
        client = get_client()
        instances = get_running_instances(client)
        if not instances:
            print("No running instances to stop")
            return 0
    else:
        instances = parse_instances(args.instances)

    if not instances:
        print("No instances specified")
        return 1

    if len(instances) > 1:
        success = run_parallel(_stop_instance_worker, instances)
    else:
        client = get_client()
        success = stop_instance(client, instances[0])
    return 0 if success else 1


def cmd_rm(args: argparse.Namespace) -> int:
    """Remove dead (non-running) containers."""
    if args.instances == ["all"]:
        client = get_client()
        instances = get_dead_instances(client)
        if not instances:
            print("No dead instances to remove")
            return 0
    else:
        instances = parse_instances(args.instances)

    if not instances:
        print("No instances specified")
        return 1

    if len(instances) > 1:
        success = run_parallel(_rm_instance_worker, instances)
    else:
        client = get_client()
        success = rm_instance(client, instances[0])

    remaining = [get_instance_config(n)["workspace"] for n in instances
                 if get_instance_config(n)["workspace"].exists()]
    if remaining:
        dirs = "\n  ".join(str(d) for d in remaining)
        print(f"Note: workspace(s) remain on disk (use 'cm clean' to remove):\n  {dirs}")

    return 0 if success else 1


def cmd_clean(args: argparse.Namespace) -> int:
    """Remove workspace directories that have no matching container."""
    client = get_client()

    # Get all container instance numbers (any state)
    containers = client.api.containers(all=True, filters={"label": "cm.managed=true"})
    container_nums = set()
    for c in containers:
        names = c.get("Names", [])
        if not names:
            continue
        try:
            container_nums.add(int(names[0].lstrip("/").split("-")[1]))
        except (IndexError, ValueError):
            continue

    # Find workspace dirs with no matching container
    if not WORKSPACES_DIR.exists():
        print("No orphaned workspaces found (use 'cm rm all' first to remove dead containers)")
        return 0
    if not WORKSPACES_DIR.is_dir():
        print(f"Error: workspace path is not a directory: {WORKSPACES_DIR}")
        return 1

    orphans = []
    for d in sorted(WORKSPACES_DIR.iterdir()):
        if not d.is_dir() or not d.name.startswith("cm."):
            continue
        try:
            n = int(d.name.split(".")[1])
        except (IndexError, ValueError):
            continue
        if n not in container_nums:
            orphans.append(d)

    if not orphans:
        print("No orphaned workspaces found (use 'cm rm all' first to remove dead containers)")
        return 0

    print(f"Orphaned workspaces ({len(orphans)}):")
    for d in orphans:
        print(f"  {d.name}/")

    try:
        response = input(f"Remove all {len(orphans)} workspace(s)? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    if response != "y":
        print("Aborted")
        return 1

    for d in orphans:
        shutil.rmtree(d)
        print(f"Removed {d.name}/")

    return 0


def cmd_ssh(args: argparse.Namespace) -> int:
    """SSH into an instance."""
    client = get_client()
    cfg = get_instance_config(args.instance)

    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return 1
    if not container or container.status != "running":
        print(f"Instance {args.instance} is not running")
        return 1

    port = get_container_ssh_port(container, cfg["port"])
    target = "me@127.0.0.1"
    identity = Path(getattr(args, "identity", None) or "~/.ssh/cm_ed25519").expanduser()
    if not identity.is_file():
        print(f"Error: SSH private key is not a file: {identity}", file=sys.stderr)
        print("Create it with: ssh-keygen -t ed25519 -f ~/.ssh/cm_ed25519", file=sys.stderr)
        print("Or pass another key with: cm ssh -i PATH N", file=sys.stderr)
        return 1
    if not os.access(identity, os.R_OK):
        print(f"Error: Cannot read SSH private key: {identity}", file=sys.stderr)
        return 1
    ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "SetEnv=LANG=en_US.UTF-8",
        "-o", "SendEnv=-LANG -LC_*",
        "-p", str(port),
        "-i", str(identity),
        "-o", "IdentitiesOnly=yes",
    ]
    ssh_args.append(target)

    try:
        os.execlp("ssh", *ssh_args)
    except FileNotFoundError:
        sys.exit("Error: ssh is not installed.")


def parse_status(status_str: str) -> tuple[str, str]:
    """Extract uptime and health from Docker status string.

    Docker status looks like "Up 2 hours" or "Up 2 hours (healthy)".
    Returns (uptime, health) tuple.
    """
    if not status_str or not status_str.startswith("Up "):
        return ("-", "-")

    # Remove "Up " prefix
    rest = status_str[3:]

    # Check for health status suffix
    if " (" in rest:
        uptime, health_part = rest.split(" (", 1)
        health = health_part.rstrip(")")
        if health.startswith("health: "):
            health = health.removeprefix("health: ")
    else:
        uptime = rest
        health = "-"

    return (uptime, health)


def cmd_list(args: argparse.Namespace) -> int:
    """List all CM instances."""
    client = get_client()

    # Low-level API returns lightweight dicts
    containers = client.api.containers(all=True, filters={"label": "cm.managed=true"})

    instances = []
    for c in containers:
        names = c.get("Names", [])
        if not names:
            continue
        name = names[0].lstrip("/")
        try:
            n = int(name.split("-")[1])
        except (IndexError, ValueError):
            continue

        state = c.get("State", "unknown")
        status = c.get("Status", "")
        uptime, health = parse_status(status)
        port = get_list_ssh_port(client, c, BASE_PORT + n)
        instances.append((n, name, state, uptime, health, port))

    if not instances:
        print("No CM instances found")
        return 0

    print(f"{'#':<4} {'Container':<12} {'Status':<12} {'Uptime':<16} {'Health':<12} {'Port':<8} {'SSH'}")
    print("-" * 88)
    for n, name, state, uptime, health, port in sorted(instances):
        ssh_cmd = f"cm ssh {n}" if state == "running" else "-"
        print(f"{n:<4} {name:<12} {state:<12} {uptime:<16} {health:<12} {port:<8} {ssh_cmd}")

    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Fast instance completion for bash (internal command)."""
    mode = args.mode
    table = args.table

    if mode == "instances":
        if table:
            # Need Docker API to get status for table display
            client = get_client()
            containers = client.api.containers(all=True, filters={"label": "cm.managed=true"})
            instances = []
            for c in containers:
                names = c.get("Names", [])
                if not names:
                    continue
                name = names[0].lstrip("/")
                try:
                    n = int(name.split("-")[1])
                except (IndexError, ValueError):
                    continue
                status = c.get("State", "unknown")
                instances.append((n, name, status))
            if instances:
                print(f"{'#':<4} {'Container':<12} {'Status':<12}")
                print("-" * 30)
                for n, name, status in sorted(instances):
                    print(f"{n:<4} {name:<12} {status:<12}")
        else:
            # Filesystem scan - instant
            instances = []
            if WORKSPACES_DIR.is_dir():
                for d in WORKSPACES_DIR.iterdir():
                    if d.is_dir() and d.name.startswith("cm."):
                        try:
                            n = int(d.name.split(".")[1])
                            instances.append(n)
                        except (IndexError, ValueError):
                            pass
            print(" ".join(str(n) for n in sorted(instances)))

    elif mode == "running":
        # Low-level Docker API
        client = get_client()
        containers = client.api.containers(
            filters={"label": "cm.managed=true", "status": "running"}
        )
        instances = []
        for c in containers:
            names = c.get("Names", [])
            if not names:
                continue
            name = names[0].lstrip("/")
            try:
                n = int(name.split("-")[1])
            except (IndexError, ValueError):
                continue
            if table:
                status = c.get("State", "running")
                instances.append((n, name, status))
            else:
                instances.append(n)
        if table:
            if instances:
                print(f"{'#':<4} {'Container':<12} {'Status':<12}")
                print("-" * 30)
                for n, name, status in sorted(instances):
                    print(f"{n:<4} {name:<12} {status:<12}")
        else:
            print(" ".join(str(n) for n in sorted(instances)))

    elif mode == "dead":
        # Low-level Docker API - filter for non-running containers
        client = get_client()
        containers = client.api.containers(all=True, filters={"label": "cm.managed=true"})
        instances = []
        for c in containers:
            state = c.get("State", "")
            if state == "running":
                continue
            names = c.get("Names", [])
            if not names:
                continue
            name = names[0].lstrip("/")
            try:
                n = int(name.split("-")[1])
            except (IndexError, ValueError):
                continue
            if table:
                instances.append((n, name, state))
            else:
                instances.append(n)
        if table:
            if instances:
                print(f"{'#':<4} {'Container':<12} {'Status':<12}")
                print("-" * 30)
                for n, name, status in sorted(instances):
                    print(f"{n:<4} {name:<12} {status:<12}")
        else:
            print(" ".join(str(n) for n in sorted(instances)))

    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    """Show logs for an instance."""
    client = get_client()
    cfg = get_instance_config(args.instance)

    try:
        container = get_managed_container(client, cfg["container"])
    except UnmanagedContainerNameError as e:
        print(e)
        return 1
    if not container:
        print(f"Instance {args.instance} does not exist")
        return 1

    # Stream logs
    try:
        for line in container.logs(stream=True, follow=True):
            print(line.decode("utf-8"), end="")
    except KeyboardInterrupt:
        print()

    return 0


def cmd_panes(args: argparse.Namespace) -> int:
    """Open tmux session with SSH panes for instances."""
    client = get_client()
    instances = parse_instances(args.instances)

    if not instances:
        print("No instances specified")
        return 1

    # Check which instances are running
    running = get_running_instances(client)
    not_running = [n for n in instances if n not in running]
    if not_running:
        print(f"Warning: Instance(s) {', '.join(map(str, not_running))} not running")
        instances = [n for n in instances if n in running]
        if not instances:
            print("No running instances to connect to")
            return 1

    session_name, existed = get_next_session_name()
    if existed:
        print(f"Warning: Existing session found, creating '{session_name}'")

    # Create new session with first instance
    first = instances[0]
    cm_path = shlex.quote(str(get_cm_command_path()))
    pane_cmd = f"{cm_path} ssh {first} || exec $SHELL"
    run_tmux(["new-session", "-d", "-s", session_name, "-n", session_name,
                    pane_cmd], check=True)
    run_tmux(["set-option", "-t", session_name, "detach-on-destroy", "off"], check=True)
    run_tmux(["set-option", "-t", session_name, "set-titles", "on"], check=True)
    run_tmux(["set-option", "-t", session_name, "set-titles-string", session_name], check=True)
    run_tmux(["setw", "-t", session_name, "automatic-rename", "off"], check=True)

    # Split panes for remaining instances
    for i, n in enumerate(instances[1:], start=1):
        pane_cmd = f"{cm_path} ssh {n} || exec $SHELL"
        run_tmux(["split-window", "-t", session_name, pane_cmd], check=True)
        # Rebalance layout after each split to prevent "no space for new pane"
        run_tmux(["select-layout", "-t", session_name, "tiled"],
                       check=True)

    # Enable synchronized panes if requested
    if args.sync:
        run_tmux(["setw", "-t", session_name, "synchronize-panes", "on"],
                       check=True)

    # Switch or attach to session (replaces current process)
    if os.environ.get("TMUX"):
        exec_tmux(["switch-client", "-t", session_name])
    else:
        exec_tmux(["attach", "-t", session_name])


def cmd_win(args: argparse.Namespace) -> int:
    """Open tmux session with SSH windows for instances."""
    client = get_client()
    instances = parse_instances(args.instances)

    if not instances:
        print("No instances specified")
        return 1

    # Check which instances are running
    running = get_running_instances(client)
    not_running = [n for n in instances if n not in running]
    if not_running:
        print(f"Warning: Instance(s) {', '.join(map(str, not_running))} not running")
        instances = [n for n in instances if n in running]
        if not instances:
            print("No running instances to connect to")
            return 1

    session_name, existed = get_next_session_name()
    if existed:
        print(f"Warning: Existing session found, creating '{session_name}'")

    # Create new session with first window
    first = instances[0]
    cm_path = shlex.quote(str(get_cm_command_path()))
    window_name = f"{session_name}-w{first}"
    pane_cmd = f"{cm_path} ssh {first} || exec $SHELL"
    run_tmux(["new-session", "-d", "-s", session_name,
                    "-n", window_name, pane_cmd], check=True)
    run_tmux(["set-option", "-t", session_name, "detach-on-destroy", "off"], check=True)
    run_tmux(["set-option", "-t", session_name, "set-titles", "on"], check=True)
    run_tmux(["set-option", "-t", session_name, "set-titles-string", session_name], check=True)
    run_tmux(["setw", "-t", session_name, "automatic-rename", "off"], check=True)

    # Create additional windows
    for n in instances[1:]:
        window_name = f"{session_name}-w{n}"
        pane_cmd = f"{cm_path} ssh {n} || exec $SHELL"
        run_tmux(["new-window", "-t", f"{session_name}:",
                        "-n", window_name, pane_cmd], check=True)

    # Enable synchronized panes if requested
    if args.sync:
        run_tmux(["setw", "-t", session_name, "synchronize-panes", "on"],
                       check=True)

    # Switch or attach to session (replaces current process)
    if os.environ.get("TMUX"):
        exec_tmux(["switch-client", "-t", session_name])
    else:
        exec_tmux(["attach", "-t", session_name])


def cmd_kill(args: argparse.Namespace) -> int:
    """Kill cm tmux session(s)."""
    # If specific sessions provided, kill them directly
    if args.sessions:
        for session in args.sessions:
            result = run_tmux(["kill-session", "-t", session],
                                    capture_output=True)
            if result.returncode != 0:
                print(f"Session '{session}' not found")
            else:
                print(f"Killed session '{session}'")
        return 0

    # No sessions specified - find all cm sessions
    result = run_tmux(["list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print("No tmux sessions found")
        return 1

    sessions = result.stdout.strip().split("\n")
    cm_sessions = [s for s in sessions if s == "cm" or s.startswith("cm-")]

    if not cm_sessions:
        print("No cm sessions found")
        return 1

    # Prompt for confirmation
    print(f"Sessions to kill: {', '.join(cm_sessions)}")
    try:
        response = input("Kill all? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    if response != "y":
        print("Aborted")
        return 1

    for session in cm_sessions:
        run_tmux(["kill-session", "-t", session], capture_output=True)
        print(f"Killed session '{session}'")

    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Toggle synchronize-panes for tmux session(s)."""
    state = "on" if args.state == "on" else "off"

    # If specific sessions provided, use those
    if args.sessions:
        for session in args.sessions:
            result = run_tmux(["has-session", "-t", session], capture_output=True)
            if result.returncode != 0:
                print(f"Session '{session}' not found")
            else:
                run_tmux(["setw", "-t", session, "synchronize-panes", state],
                         check=True)
                print(f"synchronize-panes {state} for '{session}'")
        return 0

    # No sessions specified - find all cm sessions
    result = run_tmux(["list-sessions", "-F", "#{session_name}"],
                      capture_output=True, text=True)
    if result.returncode != 0:
        print("No tmux sessions found")
        return 1

    sessions = result.stdout.strip().split("\n")
    cm_sessions = [s for s in sessions if s == "cm" or s.startswith("cm-")]

    if not cm_sessions:
        print("No cm sessions found. Use 'cm pan' or 'cm win' first.")
        return 1

    for session in cm_sessions:
        run_tmux(["setw", "-t", session, "synchronize-panes", state], check=True)
        print(f"synchronize-panes {state} for '{session}'")

    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print version information."""
    print(f"cm {VERSION}")
    return 0


def cmd_autocomplete(args: argparse.Namespace) -> int:
    """Print bash completion script to stdout."""
    script = '''\
_cm_completions() {
    local cur prev words cword
    _init_completion || return

    local commands="start stop restart rm clean ssh list logs pan win kill sync version"

    if [[ $cword -eq 1 ]]; then
        COMPREPLY=($(compgen -W "$commands" -- "$cur"))
        return
    fi

    local cmd="${words[1]}"

    # Helper: filter out words already on command line
    _filter_used() {
        local item
        for item; do
            local used=0
            local w
            for w in "${words[@]}"; do
                [[ "$w" == "$item" ]] && { used=1; break; }
            done
            [[ $used -eq 0 ]] && echo "$item"
        done
    }

    # Helper: show formatted table and return numbers
    _cm_complete_instances() {
        local mode="$1"
        local instances
        instances=$(cm _complete "$mode" 2>/dev/null)
        if [[ -z "$cur" && -n "$instances" ]]; then
            echo >/dev/tty
            cm _complete "$mode" --table 2>/dev/null >/dev/tty
            printf '\n%s ' "${words[*]}" >/dev/tty
        fi
        echo "$instances"
    }

    case "$cmd" in
        ssh|logs)
            # Single instance number (running only)
            local instances
            instances=$(_cm_complete_instances running)
            COMPREPLY=($(compgen -W "$instances" -- "$cur"))
            ;;
        start)
            # Multiple instance numbers (no duplicates)
            local instances
            instances=$(_cm_complete_instances instances)
            COMPREPLY=($(compgen -W "$(_filter_used $instances)" -- "$cur"))
            ;;
        pan|win)
            # Flags or running instance numbers (no duplicates)
            if [[ "$cur" == -* ]]; then
                COMPREPLY=($(compgen -W "--sync -s" -- "$cur"))
            else
                local instances
                instances=$(_cm_complete_instances running)
                COMPREPLY=($(compgen -W "$(_filter_used $instances)" -- "$cur"))
            fi
            ;;
        stop)
            # Multiple running instances plus "all" (no duplicates)
            local instances
            instances=$(_cm_complete_instances running)
            COMPREPLY=($(compgen -W "all $(_filter_used $instances)" -- "$cur"))
            ;;
        restart)
            # Multiple instance numbers plus "all" (no duplicates)
            local instances
            instances=$(_cm_complete_instances instances)
            COMPREPLY=($(compgen -W "all $(_filter_used $instances)" -- "$cur"))
            ;;
        rm)
            # Multiple instance numbers plus "all" for dead containers (no duplicates)
            local instances
            instances=$(_cm_complete_instances dead)
            COMPREPLY=($(compgen -W "all $(_filter_used $instances)" -- "$cur"))
            ;;
        kill)
            # Tmux session names (no duplicates)
            local sessions
            sessions=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | grep -E '^cm(-|$)')
            COMPREPLY=($(compgen -W "$(_filter_used $sessions)" -- "$cur"))
            ;;
        sync)
            if [[ $cword -eq 2 ]]; then
                # First arg: on or off
                COMPREPLY=($(compgen -W "on off" -- "$cur"))
            else
                # Subsequent args: tmux session names (no duplicates)
                local sessions
                sessions=$(tmux list-sessions -F "#{session_name}" 2>/dev/null | grep -E '^cm(-|$)')
                COMPREPLY=($(compgen -W "$(_filter_used $sessions)" -- "$cur"))
            fi
            ;;
    esac
}

complete -F _cm_completions cm
'''
    print(script)
    return 0


def main() -> int:
    # Handle internal completion command before argparse (hidden from help)
    if len(sys.argv) >= 2 and sys.argv[1] == "_complete":
        mode = sys.argv[2] if len(sys.argv) > 2 else "instances"
        table = "--table" in sys.argv or "-t" in sys.argv
        args = argparse.Namespace(mode=mode, table=table)
        return cmd_complete(args)

    parser = argparse.ArgumentParser(description="Manage CM instances")
    parser.add_argument("--version", action="version", version=f"cm {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = subparsers.add_parser("start", help="Start instance(s)")
    p_start.add_argument("instances", nargs="+", metavar="N",
                         help="Instance number(s): 1, 1-5, or 1 3 5")
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop instance(s)")
    p_stop.add_argument("instances", nargs="+", metavar="N",
                        help="Instance number(s): 1, 1-5, 1 3 5, or 'all'")
    p_stop.set_defaults(func=cmd_stop)

    # restart
    p_restart = subparsers.add_parser("restart", help="Restart instance(s)")
    p_restart.add_argument("instances", nargs="+", metavar="N",
                           help="Instance number(s): 1, 1-5, 1 3 5, or 'all'")
    p_restart.set_defaults(func=cmd_restart)

    # rm
    p_rm = subparsers.add_parser("rm", help="Remove dead (non-running) container(s)")
    p_rm.add_argument("instances", nargs="+", metavar="N",
                      help="Instance number(s): 1, 1-5, 1 3 5, or 'all'")
    p_rm.set_defaults(func=cmd_rm)

    # clean
    p_clean = subparsers.add_parser("clean", help="Remove orphaned workspace directories")
    p_clean.set_defaults(func=cmd_clean)

    # ssh
    p_ssh = subparsers.add_parser("ssh", help="SSH into an instance")
    p_ssh.add_argument("-i", "--identity", metavar="PATH",
                       help="SSH private key to use (default: ~/.ssh/cm_ed25519)")
    p_ssh.add_argument("instance", type=int, metavar="N",
                       help="Instance number")
    p_ssh.set_defaults(func=cmd_ssh)

    # list
    p_list = subparsers.add_parser("list", help="List all instances")
    p_list.set_defaults(func=cmd_list)

    # logs
    p_logs = subparsers.add_parser("logs", help="Show logs for an instance")
    p_logs.add_argument("instance", type=int, metavar="N",
                        help="Instance number")
    p_logs.set_defaults(func=cmd_logs)

    # pan
    p_panes = subparsers.add_parser("pan", help="Open tmux session with SSH panes")
    p_panes.add_argument("instances", nargs="+", metavar="N",
                         help="Instance number(s): 1, 1-5, or 1 3 5")
    p_panes.add_argument("--sync", "-s", action="store_true",
                         help="Enable synchronize-panes")
    p_panes.set_defaults(func=cmd_panes)

    # win
    p_win = subparsers.add_parser("win", help="Open tmux session with SSH windows")
    p_win.add_argument("instances", nargs="+", metavar="N",
                       help="Instance number(s): 1, 1-5, or 1 3 5")
    p_win.add_argument("--sync", "-s", action="store_true",
                       help="Enable synchronize-panes")
    p_win.set_defaults(func=cmd_win)

    # kill
    p_kill = subparsers.add_parser("kill", help="Kill cm tmux session(s)")
    p_kill.add_argument("sessions", nargs="*", metavar="SESSION",
                        help="Session name(s) to kill (default: all, with confirmation)")
    p_kill.set_defaults(func=cmd_kill)

    # sync
    p_sync = subparsers.add_parser("sync", help="Toggle synchronize-panes")
    p_sync.add_argument("state", choices=["on", "off"],
                        help="Enable or disable synchronize-panes")
    p_sync.add_argument("sessions", nargs="*", metavar="SESSION",
                        help="Session name(s) (default: all cm sessions)")
    p_sync.set_defaults(func=cmd_sync)

    # autocomplete
    p_autocomplete = subparsers.add_parser("autocomplete", help="Print bash completion script")
    p_autocomplete.set_defaults(func=cmd_autocomplete)

    # version
    p_version = subparsers.add_parser("version", help="Print version")
    p_version.set_defaults(func=cmd_version)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
