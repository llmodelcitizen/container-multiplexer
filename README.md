# cm (container multiplexer)

Manage multiple Docker containers (hundreds of them, if you want) with SSH access and simple tmux integration. Each instance gets its own SSH port and persistent workspace directory.

## Quick Start

After setup and image build:

```bash
cm start 1        # Start one container
cm ssh 1          # SSH into it
cm stop 1         # Stop it; the container and workspace remain
```

```bash
cm start 1-12     # Start instances 001-012
cm pan 1-6        # Open tmux panes, each SSH'd to an instance
cm sync on        # Enable synchronize-panes for cm sessions
```

## Setup

Verify Docker is installed and running:

```bash
docker info
```

Install `tmux` and Python 3:

```bash
sudo apt install tmux python3 python3-venv
```

Or:

```bash
brew install tmux python
```

Install `cm`:

```bash
./install.sh
```

The installer creates a private `.cm-venv` with the Python Docker SDK and installs `cm` into `~/.local/bin` by default. If `~/.local/bin` is not on your `PATH`, the installer prints the shell commands to add it.

## Images

`cm start` expects a local Docker image named `cm`. That runtime image is intentionally thin so that changes to the base image can be picked up quickly:

```text
cm-bootstrap:latest -> cm-base:latest -> cm:latest
Debian + tooling       custom base       runtime entrypoint
```

### Fresh Image Build

Use this method for first-time setup, after changing `Dockerfile.base`, or when you want a fresh base from Debian packages.

Build or refresh the bootstrap image from the current Debian base image and package list:

```bash
docker build --pull --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
```

Create `cm-base:latest` from a temporary container. Make any package or config changes inside the container, or exit immediately if you do not need changes:

```bash
docker run -it --user me --name cm-mod cm-bootstrap:latest /bin/bash
```

Commit the base image:

```bash
docker commit cm-mod cm-base:latest
```

Remove the temporary container:

```bash
docker rm cm-mod
```

Finally, build the runtime image. You can also run this command after changing `Dockerfile` or `entrypoint.sh`, keeping the existing `cm-base:latest`:

```bash
docker build -t cm .
```

### Quick Base Updates

Once `cm-base:latest` exists, you can use this faster path for ad-hoc changes. It updates the base image directly, then rebuilds the thin runtime image, usually avoiding the slower Debian/package rebuild.

Start a temporary container from the current base image:

```bash
docker run -it --user me --name cm-mod cm-base:latest /bin/bash
```

Make the changes inside that shell. For example:

```bash
sudo apt update && sudo apt install -y <package>
```

Exit the shell, then commit the container back over `cm-base:latest` and remove the temporary container:

```bash
docker commit cm-mod cm-base:latest && docker rm cm-mod
```

Rebuild `cm` so new instances use the updated base:

```bash
docker build -t cm .
```

Alternatively, if the useful change already exists in a running `cm` instance, you can commit that instance instead:

```bash
docker commit cm-001 cm-base:latest
docker build -t cm .
```

Remember that image changes apply only to newly created containers. To move an instance to the new image, stop it, remove the stopped container with `cm rm N`, then start it again. The workspace remains unless you run `cm clean`.

### Apple Silicon

On Apple Silicon Macs, Docker builds native `linux/arm64` images by default. That avoids Intel/AMD64 emulation, so local builds and containers are usually faster, and the commands above work as-is.

If you specifically need Intel/AMD64 images, use `--platform linux/amd64` consistently:

```bash
# amd64 bootstrap image
docker build --platform linux/amd64 --pull --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
```

Create `cm-base:latest` from a temporary container. Make any package or config changes inside the container, or exit immediately if you do not need changes:

```bash
docker run --platform linux/amd64 -it --user me --name cm-mod cm-bootstrap:latest /bin/bash
```

Then commit and remove the temporary container with the same commands shown above.

```bash
docker commit cm-mod cm-base:latest && docker rm cm-mod
```

Finally, build the runtime image. You can also run this command after changing `Dockerfile` or `entrypoint.sh`, keeping the existing `cm-base:latest`:

```
docker build --platform linux/amd64 -t cm .
```

## SSH Keys

`cm` mounts an `authorized_keys` file into each container at `/home/me/.ssh/authorized_keys`.

During install, if `./authorized_keys` exists, `install.sh` creates `$INSTALL_DIR/authorized_keys` as a symlink to it (`$INSTALL_DIR` is `~/.local/bin` by default).

If `./authorized_keys` does not exist during install, `install.sh` does not create that symlink. The installed `cm` will then use `~/.ssh/authorized_keys` unless you later create `$INSTALL_DIR/authorized_keys`.

On each run, `cm` chooses the source file in this order:

1. `authorized_keys` in the same directory as the `cm` program being executed.
   - Running from this checkout: `./authorized_keys`
   - Running an installed copy: `$INSTALL_DIR/authorized_keys`, which is `~/.local/bin/authorized_keys` by default
2. `~/.ssh/authorized_keys`

`cm ssh N` connects as `me@127.0.0.1` on the instance's published SSH port. You do not need to create a `Host cm` entry in your ssh config. To force a private key, use:

```bash
cm ssh -i ~/.ssh/id_ed25519 1
```

or set `CM_SSH_IDENTITY=/path/to/key`.

## Shell Completion

`cm autocomplete` currently prints bash completion only, and it depends on `bash-completion`. zsh completion is not implemented.

Install `bash-completion` if needed:

```bash
sudo apt install bash-completion
```

Or:

```bash
brew install bash-completion@2
```

Add this to `~/.bashrc`, assuming you have used the `cm` installer:

```bash
command -v cm &>/dev/null && source <(cm autocomplete)
```

Reload bash configuration:

```bash
source ~/.bashrc
```

## Commands

Use `cm <command> -h` for command-specific help. Instance arguments support single numbers, ranges like `1-5`, and repeated values like `1 3 5`. Instance numbers must be `1` through `499`.

```bash
# Start/stop        (containers persist when stopped, like docker)
cm start 1          # Start instance (creates new or starts existing stopped container)
cm start 1-50       # Start 50 container instances (!)
cm stop 1           # Stop a container (keeps it for later restart)
cm stop all         # Stop all running instances
cm restart all      # Restart all running instances
cm rm 1             # Remove a non-running container
cm rm all           # Remove all non-running containers
cm clean            # Remove orphaned workspace directories

# Connect
cm list             # List all instances with status, health, port, and SSH command
cm ssh 1            # SSH into an individual instance
cm logs 1           # Stream container logs like "docker logs -f"

# Tmux sessions     (for working with many container instances)
cm pan 1-9          # Use split panes, each SSH'd to an instance
cm pan 1-9 --sync   # Same, with synchronize-panes enabled
cm win 1-2          # Use tmux windows instead of panes
cm kill             # Kill cm sessions (all by default, with confirmation)
cm sync on          # Enable synchronize-panes for cm sessions

# Version
cm version          # Print version
```

More examples:

```bash
cm start 1 3 5      # Start a specific set of instances
cm stop 1-12        # Stop a range of running instances
cm restart 7        # Restart one instance
cm rm 1-12          # Remove a range of non-running containers
cm pan 1-9 -s       # Short form of --sync
cm sync off         # Disable synchronize-panes for cm sessions
cm kill cm-s1       # Kill one named cm tmux session
cm sync off cm-s1   # Disable synchronize-panes for one named cm session
```

## Behavior Notes

- Multi-instance `start`, `stop`, `restart`, and `rm` operations run in parallel.
- SSH ports default to `2200 + N`, but `cm` retries higher ports when a port is busy. Use `cm list` instead of assuming the port.
- Workspaces live under `workspaces/cm.001/`, `workspaces/cm.002/`, and so on, mounted at `/home/me/workspace`.
- Tmux sessions are named `cm-s1`, `cm-s2`, and so on.
- Clean exit from a container shell closes the tmux pane. Connection errors drop to a host shell.
- When launched from an existing tmux or byobu session, closing the last cm pane returns to the parent session.
