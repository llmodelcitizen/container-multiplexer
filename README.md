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
cm pan 1-6        # Open tmux panes, each SSH'd to a running instance
cm sync on        # Enable synchronize-panes for cm tmux sessions
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

The installer prompts for an install directory (`~/.local/bin` by default), creates a private `.cm-venv` there with the Python Docker SDK, copies the CLI as `cm.py`, writes a `cm` wrapper, and symlinks `workspaces/` back to this checkout. When creating a symlink, it replaces an existing symlink but refuses to overwrite an existing non-symlink path. If the chosen install directory is not on your `PATH`, the installer prints the shell commands to add it.

## Images

`cm start` needs a local Docker image named `cm` (`cm:latest`) when it creates a new container. That runtime image is intentionally thin so that changes to the base image can be picked up quickly:

```text
cm-bootstrap:latest -> cm-base:latest -> cm:latest
Debian + tooling       custom base       runtime entrypoint
```

### Fresh Image Build

Use this method for first-time setup, after changing `Dockerfile.base`, or when you want a fresh base from Debian packages.

Build or refresh the bootstrap image from `debian:13-slim` and the package list in `Dockerfile.base`:

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

Alternatively, if the useful change already exists in a running `cm` instance, you can commit that instance instead. This captures the container filesystem, not the mounted workspace at `/home/me/workspace`. Reset the runtime entrypoint while saving it as the base image:

```bash
docker commit --change 'ENTRYPOINT []' cm-001 cm-base:latest
docker build -t cm .
```

Remember that image changes apply only to newly created containers. To move an instance to the new image, stop it, remove the stopped container with `cm rm N`, then start it again. The workspace remains unless you run `cm clean` while no container exists for that instance.

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

`cm` mounts the selected `authorized_keys` file into each container at `/tmp/cm_authorized_keys`; `entrypoint.sh` then installs it as `/home/me/.ssh/authorized_keys`.

During install, if a project-root `authorized_keys` exists, `install.sh` creates `$INSTALL_DIR/authorized_keys` as a symlink to it (`$INSTALL_DIR` is `~/.local/bin` by default).

If a project-root `authorized_keys` does not exist during install, `install.sh` does not create that symlink. With no `$INSTALL_DIR/authorized_keys` present, the installed `cm` falls back to `~/.ssh/authorized_keys`; creating `$INSTALL_DIR/authorized_keys` later overrides that fallback.

On each run, `cm` chooses the source file in this order. The chosen source must be a non-empty regular file.

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

Add this to your bash startup file, assuming you have used the `cm` installer. On Linux this is usually `~/.bashrc`; for bash login shells on macOS it may be `~/.bash_profile`.

```bash
command -v cm &>/dev/null && source <(cm autocomplete)
```

Reload the file you changed, for example:

```bash
source ~/.bashrc
```

## Commands

Use `cm <command> -h` for command-specific help. Commands that accept instance lists (`start`, `stop`, `restart`, `rm`, `pan`, and `win`) support single numbers, ranges like `1-5`, and repeated values like `1 3 5`; those list-style instance numbers must be `1` through `499`. `stop`, `restart`, and `rm` also accept `all`. `ssh` and `logs` accept one instance number.

```bash
# Start/stop        (containers persist when stopped, like docker)
cm start 1          # Start instance (creates new or starts existing stopped container)
cm start 1-50       # Start 50 container instances (!)
cm stop 1           # Stop a container (keeps it for later restart)
cm stop all         # Stop all running instances
cm restart all      # Restart all running instances
cm rm 1             # Remove a non-running container
cm rm all           # Remove all non-running containers
cm clean            # Prompt to remove orphaned workspace directories

# Connect
cm list             # List all instances with status, health, port, and SSH command
cm ssh 1            # SSH into an individual instance
cm logs 1           # Stream container logs like "docker logs -f"

# Tmux sessions     (for working with many container instances)
cm pan 1-9          # Use split panes, each SSH'd to a running instance
cm pan 1-9 --sync   # Same, with synchronize-panes enabled
cm win 1-2          # Use tmux windows instead of panes for running instances
cm kill             # Kill cm tmux sessions (all by default, with confirmation)
cm sync on          # Enable synchronize-panes for cm tmux sessions

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
cm sync off         # Disable synchronize-panes for cm tmux sessions
cm kill cm-s1       # Kill one named cm tmux session
cm sync off cm-s1   # Disable synchronize-panes for one named cm tmux session
```

## Behavior Notes

- Multi-instance `start`, `stop`, `restart`, and `rm` operations run in parallel.
- SSH ports bind to `127.0.0.1` and default to `2200 + N`, but `cm` retries higher ports when a port is busy. Use `cm list` instead of assuming the port.
- Workspaces live under the `workspaces/` directory next to the executed CLI (`workspaces/cm.001/`, `workspaces/cm.002/`, and so on) and are mounted at `/home/me/workspace`. The installer makes that installed `workspaces/` path a symlink back to this checkout.
- Tmux sessions are named `cm-s1`, `cm-s2`, and so on.
- Clean exit from a container shell closes the tmux pane. Connection errors drop to a host shell.
- When launched from an existing tmux or byobu session, `cm pan` and `cm win` switch the current tmux client into the new session; when that session is destroyed, tmux stays attached to another available session instead of detaching.
