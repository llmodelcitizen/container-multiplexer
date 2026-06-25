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

Finally, build the runtime image:

```bash
docker build -t cm .
```

After changing only `Dockerfile` or `entrypoint.sh`, rebuild just the runtime image with the cached `cm-base:latest`.

### Quick Base Updates

Once `cm-base:latest` already exists, use this faster path for ad-hoc changes such as installing a package or tweaking shell configuration. It updates the base image directly, then rebuilds the thin runtime image, usually avoiding the slower Debian/package rebuild.

Start a temporary container from the current base image:

```bash
docker run -it --user me --name cm-mod cm-base:latest /bin/bash
```

Make the changes inside that shell. For example:

```bash
sudo apt-get update
sudo apt-get install -y <package>
```

Exit the shell, then commit the container back over `cm-base:latest` and remove the temporary container:

```bash
docker commit cm-mod cm-base:latest
docker rm cm-mod
```

Rebuild `cm` so new instances use the updated base:

```bash
docker build -t cm .
```

If the useful change already exists in a running `cm` instance, you can commit that instance instead:

```bash
docker commit cm-001 cm-base:latest
docker build -t cm .
```

Image changes apply only to newly created containers. To move an instance to the new image, stop it, remove the stopped container with `cm rm N`, then start it again. The workspace remains unless you run `cm clean`.

### Apple Silicon

On Apple Silicon Macs, Docker builds native `linux/arm64` images by default. That is the fastest local path, and the commands above work as-is.

If you specifically need Intel/AMD64 images, use `--platform linux/amd64` consistently:

```bash
# Build an amd64 bootstrap image
docker build --platform linux/amd64 --pull --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
```

```bash
# Run the amd64 temporary base container
docker run --platform linux/amd64 -it --user me --name cm-mod cm-bootstrap:latest /bin/bash
```

Then commit and remove the temporary container with the same commands shown above.

```bash
# Build the amd64 runtime image
docker build --platform linux/amd64 -t cm .
```

## SSH Keys

`cm` mounts an `authorized_keys` file into each container. It uses an `authorized_keys` file next to the `cm` script when present; in this checkout, that is `./authorized_keys`. Otherwise it uses `~/.ssh/authorized_keys`.

`cm ssh N` connects as `me@127.0.0.1` on the instance's published SSH port. No `Host cm` entry is needed. To force a private key, use:

```bash
cm ssh -i ~/.ssh/id_ed25519 1
```

or set `CM_SSH_IDENTITY=/path/to/key`.

## Shell Completion

`cm autocomplete` currently prints bash completion only, and it depends on `bash-completion`. zsh completion is not implemented.

Linux bash:

```bash
# Install bash-completion
sudo apt install bash-completion
```

```bash
# Append cm's bash completion
cm autocomplete >> ~/.bashrc
```

```bash
# Reload bash configuration
source ~/.bashrc
```

macOS bash:

```bash
# Install Homebrew bash-completion
brew install bash-completion@2
```

```bash
# Enable Homebrew bash-completion
echo '[[ -r "$(brew --prefix)/etc/profile.d/bash_completion.sh" ]] && . "$(brew --prefix)/etc/profile.d/bash_completion.sh"' >> ~/.bash_profile
```

```bash
# Append cm's bash completion
cm autocomplete >> ~/.bash_profile
```

```bash
# Reload bash profile
source ~/.bash_profile
```

## Commands

Use `cm <command> -h` for command-specific help. Instance arguments support single numbers, ranges like `1-5`, and repeated values like `1 3 5`. Instance numbers must be `1` through `499`.

- `cm start 1-12`: create or start instances.
- `cm stop 1` / `cm stop all`: stop running containers without deleting them.
- `cm restart 1` / `cm restart all`: restart named instances; `all` targets currently running instances.
- `cm rm 1` / `cm rm all`: remove non-running containers; workspaces stay on disk.
- `cm clean`: remove workspace directories with no matching container.
- `cm list`: show instances, status, uptime, health, SSH port, and SSH command.
- `cm ssh 1`: SSH into a running instance.
- `cm logs 1`: stream container logs.
- `cm pan 1-6` / `cm win 1-6`: open tmux panes or windows for running instances.
- `cm pan 1-6 --sync`: open panes with synchronize-panes enabled.
- `cm sync on` / `cm sync off`: toggle synchronize-panes for cm tmux sessions.
- `cm kill`: kill cm tmux sessions, with confirmation when no session names are given.
- `cm version`: print the installed version.

## Behavior Notes

- Multi-instance `start`, `stop`, `restart`, and `rm` operations run in parallel.
- SSH ports default to `2200 + N`, but `cm` retries higher ports when a port is busy. Use `cm list` instead of assuming the port.
- Workspaces live under `workspaces/cm.001/`, `workspaces/cm.002/`, and so on, mounted at `/home/me/workspace`.
- Tmux sessions are named `cm-s1`, `cm-s2`, and so on.
- Clean exit from a container shell closes the tmux pane. Connection errors drop to a host shell.
- When launched from an existing tmux or byobu session, closing the last cm pane returns to the parent session.
