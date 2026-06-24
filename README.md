# cm (container multiplexer)

Manage multiple Docker containers (hundreds of them, if you want) with SSH access and simple tmux integration. Each instance gets its own SSH port and persistent workspace directory.

## Quick Start

```bash
./cm start 1        # Start a single container
./cm ssh 1          # SSH into it
./cm stop 1         # Stop it

# Or work with many container instances at once
./cm start 1-12     # Start twelve container instances (001-012)
./cm pan 1-6        # Tmux session with split panes, each SSH'd
./cm sync on        # Enable synchronize-panes for cm sessions
```

## Setup

### macOS

1. Install and start Docker Desktop. Homebrew users can install it with:
   ```bash
   brew install --cask docker
   open -a Docker
   ```
   Wait for Docker Desktop to finish starting, then verify the engine is reachable:
   ```bash
   docker info
   ```

2. Install `tmux` and Python 3:
   ```bash
   brew install tmux python
   ```

3. Install the Python Docker SDK for the same interpreter that runs `cm`. Use `python3 -m pip` rather than `pip3` so the package is installed for that `python3`:
   ```bash
   python3 -m pip install --user docker
   python3 -c "import docker; print(docker.__version__)"
   ```
   If your Python install requires a virtual environment, activate that environment before installing the SDK and before running `cm`.

4. macOS defaults to zsh. If you install `cm` into `~/.local/bin`, make sure zsh can find it:
   ```zsh
   mkdir -p ~/.local/bin
   grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' ~/.zshrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

5. Install `cm` into `~/.local/bin` or another directory on your `PATH`. Re-run this after a `git pull` to update the installed copy:
   ```bash
   ./install.sh
   ```

### Linux

1. Install Docker, `tmux`, and the Python Docker SDK. On Debian/Ubuntu:
   ```bash
   sudo apt install docker.io tmux python3-docker
   ```
   If your distribution does not package the SDK, install it for the interpreter that runs `cm`:
   ```bash
   python3 -m pip install --user docker
   ```

2. Start Docker and verify the engine is reachable:
   ```bash
   docker info
   ```

3. Optionally install `cm` into `~/.local/bin` or another directory on your `PATH`. Re-run this after a `git pull` to update the installed copy:
   ```bash
   ./install.sh
   ```

### SSH keys

`cm` uses your `~/.ssh/authorized_keys` (public keys only) to install SSH access inside each container automatically. No extra setup is needed if you already have one.

To use a different key set for cm containers, place an `authorized_keys` file in the project root; this overrides `~/.ssh/authorized_keys`.

`cm ssh N` connects directly as `me@127.0.0.1` on the instance's published SSH port; no `Host cm` entry is required in `~/.ssh/config`. To force a specific private key, use `cm ssh -i ~/.ssh/id_ed25519 N` or set `CM_SSH_IDENTITY=/path/to/key`.

### Shell completion

`cm autocomplete` currently prints bash completion only. It calls `_init_completion`, so bash users must install `bash-completion` before sourcing it. zsh completion is not implemented; zsh users should skip this step.

Linux bash:
```bash
sudo apt install bash-completion
./cm autocomplete >> ~/.bashrc
source ~/.bashrc
```

macOS bash:
```bash
brew install bash-completion@2
{
  echo '[[ -r "$(brew --prefix)/etc/profile.d/bash_completion.sh" ]] && . "$(brew --prefix)/etc/profile.d/bash_completion.sh"'
  ./cm autocomplete
} >> ~/.bash_profile
source ~/.bash_profile
```

### Build images

1. Build the base image:
   ```bash
   # Build bootstrap image
   docker build --no-cache -t cm-bootstrap:latest -f Dockerfile.base .

   # Run interactively to make modifications
   docker run -it --user me --name cm-mod cm-bootstrap:latest /bin/bash
   docker commit cm-mod cm-base:latest
   docker rm cm-mod
   ```

2. Build the runtime image:
   ```bash
   docker build -t cm .
   ```

### Apple Silicon image architecture

On Apple Silicon Macs, Docker builds native `linux/arm64` images by default. This is the fastest local path, and the setup commands above work as-is for native arm64 images:

```bash
docker build --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
docker build -t cm .
```

If you specifically need Intel/AMD64 images, add `--platform linux/amd64` consistently when building the bootstrap image, running the temporary image you commit, and building the runtime image:

```bash
docker build --platform linux/amd64 --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
docker run --platform linux/amd64 -it --user me --name cm-mod cm-bootstrap:latest /bin/bash
docker commit cm-mod cm-base:latest
docker rm cm-mod
docker build --platform linux/amd64 -t cm .
```

## Usage
Use `-h` for each argument to explore more
```
cm -h
usage: cm [-h] [--version] {start,stop,restart,rm,clean,ssh,list,logs,pan,win,kill,sync,autocomplete,version} ...

Manage CM instances

positional arguments:
  {start,stop,restart,rm,clean,ssh,list,logs,pan,win,kill,sync,autocomplete,version}
    start               Start instance(s)
    stop                Stop instance(s)
    restart             Restart instance(s)
    rm                  Remove dead (non-running) container(s)
    clean               Remove orphaned workspace directories
    ssh                 SSH into an instance
    list                List all instances
    logs                Show logs for an instance
    pan                 Open tmux session with SSH panes
    win                 Open tmux session with SSH windows
    kill                Kill cm tmux session(s)
    sync                Toggle synchronize-panes
    autocomplete        Print bash completion script
    version             Print version

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  ```

Example commands
```bash
# Start/stop (containers persist when stopped, like docker)
cm start 1          # Start instance (creates new or starts existing stopped container)
cm start 1-400      # Start 400 container instances (!)
cm stop 1           # Stop a container (keeps it for later restart)
cm stop all         # Stop all running instances
cm restart all      # Restart all running instances
cm rm 1             # Remove a stopped container
cm rm all           # Remove all stopped containers
cm clean            # Remove orphaned workspace directories

# Connect
cm list             # List all instances with status
cm ssh 1            # SSH into an individual instance
cm ssh -i ~/.ssh/id_ed25519 1  # SSH with a specific private key
cm logs 1           # View container logs ala "docker logs"

# Tmux sessions (for working with many container instances)
cm pan 1-9          # Use split panes, each SSH'd to an instance
cm pan 1-9 --sync   # Same, with synchronize-panes enabled
cm win 1-2          # Use tmux windows instead of panes
cm kill             # Kill sessions (ALL by default, with confirmation)
cm sync on          # Enable synchronize-panes for cm sessions

# Version
cm version          # Print version (from git tags)
```

### Tmux session behavior

- Clean exit (`ctrl-d`, `exit`, `logout`) from a container closes the pane. Connection errors drop to a host shell instead.
- When launched from an existing tmux/byobu session, closing the last cm pane returns to the parent session instead of detaching.

## Architecture

```
cm-bootstrap:latest  →  cm-base:latest  →  cm:latest
(Debian + tooling)      (+ mods)           (+ entrypoint)
```
- **Multithreading**: Instance operations run in parallel for fast execution
- **SSH Ports**: 2201, 2202, ... (2200 + N): auto-retries if port in use; do not assume the port number is aligned with the container instance ID
- **Workspaces**: `workspaces/cm.001/`, `workspaces/cm.002/`, ... (mounted at `/home/me/workspace`)
- **Tmux 's'essions**: `cm-s1`, `cm-s2`, ... (created by `pan`/`win` commands)


## Rebuilding Images

```bash
# After changing Dockerfile.base (rare)
docker build --no-cache -t cm-bootstrap:latest -f Dockerfile.base .
# Then commit to cm-base:latest

# On Apple Silicon, add --platform linux/amd64 to both builds only if you
# need amd64 images instead of the native arm64 default.

# After changing Dockerfile or entrypoint.sh (fast, uses cm-base cache)
docker build -t cm .
```

## Updating cm-base Manually

To make ad-hoc changes (install a package, tweak config) without rebuilding from scratch:

1. Run a temporary container from the current base image:
   ```bash
   docker run -it --user me --name cm-mod cm-base:latest /bin/bash
   ```

2. Make your changes inside the container (e.g. `sudo apt-get install -y <package>`), then exit.

3. Commit the modified container as the new base image and clean up:
   ```bash
   docker commit cm-mod cm-base:latest
   docker rm cm-mod
   ```

4. Rebuild the runtime image so it picks up the new base:
   ```bash
   docker build -t cm .
   ```

Alternatively, you can commit a running instance directly:

```bash
docker commit cm-001 cm-base:latest
docker build -t cm .
```

Running containers are unaffected — restart them to use the updated image.
