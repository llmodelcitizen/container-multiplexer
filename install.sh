#!/bin/bash
# Install cm to a directory in PATH

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/.local/bin"
CM_HOME="$HOME/.cm"
CM_WORKSPACES_DIR="$CM_HOME/workspaces"
AUTHORIZED_KEYS="$CM_HOME/authorized_keys"
DEFAULT_PUBLIC_KEY="$HOME/.ssh/cm_ed25519.pub"

path_export_value() {
    local dir="$1"

    if [[ "$dir" == "$HOME" ]]; then
        printf '$HOME'
    elif [[ "$dir" == "$HOME/"* ]]; then
        printf '$HOME/%s' "${dir#"$HOME/"}"
    else
        printf '%s' "$dir"
    fi
}

print_path_guidance() {
    local dir="$1"
    local shell_name
    local rc_file
    local path_value

    shell_name="$(basename "${SHELL:-sh}")"
    path_value="$(path_export_value "$dir")"

    echo "Note: $dir is not in your PATH."
    echo "Add it with:"

    if [[ "${ZSH_VERSION:-}" || "$shell_name" == "zsh" ]]; then
        rc_file="$HOME/.zshrc"
    elif [[ "$shell_name" == "bash" ]]; then
        if [[ "$OSTYPE" == darwin* ]]; then
            rc_file="$HOME/.bash_profile"
        else
            rc_file="$HOME/.bashrc"
        fi
    else
        echo "  export PATH=\"$path_value:\$PATH\""
        echo "Add that line to your shell startup file."
        return
    fi

    echo "  echo 'export PATH=\"$path_value:\$PATH\"' >> \"$rc_file\""
    echo "  source \"$rc_file\""
}

# --- Uninstall mode ---
if [[ "$1" == "--uninstall" ]]; then
    echo "CM Uninstaller"
    echo "=============="
    echo
    read -p "Install directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
    INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
    INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

    removed=0
    for f in cm cm.py workspaces authorized_keys; do
        target="$INSTALL_DIR/$f"
        if [[ -L "$target" || -f "$target" ]]; then
            rm -f "$target"
            echo "Removed $target"
            ((removed++))
        elif [[ -e "$target" ]]; then
            echo "Keeping non-file path: $target"
        fi
    done

    venv="$INSTALL_DIR/.cm-venv"
    if [[ -e "$venv" || -L "$venv" ]]; then
        rm -rf "$venv"
        echo "Removed $venv"
        ((removed++))
    fi

    if [[ $removed -eq 0 ]]; then
        echo "Nothing to remove in $INSTALL_DIR"
    else
        echo
        echo "Uninstalled successfully!"
    fi
    exit 0
fi

# --- Install mode ---
echo "CM Installer"
echo "============"
echo
echo "This will:"
echo "  1. Create ~/.cm, ~/.cm/workspaces, and ~/.cm/authorized_keys"
echo "  2. Create a private Python virtual environment with the Docker SDK"
echo "  3. Install a 'cm' wrapper and 'cm.py' script to your chosen directory"
echo
read -p "Install directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"

# Expand ~
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

if [[ -e "$CM_HOME" && ! -d "$CM_HOME" ]]; then
    echo "Error: $CM_HOME exists and is not a directory."
    exit 1
fi
mkdir -p "$CM_WORKSPACES_DIR"
chmod 700 "$CM_HOME" "$CM_WORKSPACES_DIR"

if [[ ! -s "$AUTHORIZED_KEYS" ]]; then
    if [[ ! -s "$DEFAULT_PUBLIC_KEY" ]]; then
        echo "Error: $AUTHORIZED_KEYS is missing or empty, and $DEFAULT_PUBLIC_KEY was not found."
        echo "Create the default cm SSH key first:"
        echo "  mkdir -p ~/.ssh"
        echo "  chmod 700 ~/.ssh"
        echo "  ssh-keygen -t ed25519 -f ~/.ssh/cm_ed25519"
        echo "  chmod 400 ~/.ssh/cm_ed25519"
        exit 1
    fi

    cp "$DEFAULT_PUBLIC_KEY" "$AUTHORIZED_KEYS"
    chmod 600 "$AUTHORIZED_KEYS"
    echo "Created $AUTHORIZED_KEYS from $DEFAULT_PUBLIC_KEY"
fi
if [[ ! -f "$AUTHORIZED_KEYS" ]]; then
    echo "Error: $AUTHORIZED_KEYS is not a file."
    exit 1
fi
if [[ ! -s "$AUTHORIZED_KEYS" ]]; then
    echo "Error: $AUTHORIZED_KEYS is empty."
    echo "Create the default cm SSH key first:"
    echo "  mkdir -p ~/.ssh"
    echo "  chmod 700 ~/.ssh"
    echo "  ssh-keygen -t ed25519 -f ~/.ssh/cm_ed25519"
    echo "  chmod 400 ~/.ssh/cm_ed25519"
    exit 1
fi
if [[ ! -r "$AUTHORIZED_KEYS" ]]; then
    echo "Error: $AUTHORIZED_KEYS is not readable."
    exit 1
fi
chmod 600 "$AUTHORIZED_KEYS"

# Create install directory if needed
if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "Creating $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
fi

VENV_DIR="$INSTALL_DIR/.cm-venv"
CM_SCRIPT="$INSTALL_DIR/cm.py"
CM_WRAPPER="$INSTALL_DIR/cm"

PYTHON="${CM_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Error: $PYTHON not found. Install Python 3 or set CM_PYTHON=/path/to/python3."
    exit 1
fi

echo "Creating Python virtual environment at $VENV_DIR"
"$PYTHON" -m venv "$VENV_DIR"

echo "Installing Python Docker SDK"
"$VENV_DIR/bin/python" -m pip install --upgrade docker

# Copy cm script and install a wrapper that always uses the managed venv.
echo "Copying cm.py to $INSTALL_DIR/"
rm -f "$CM_SCRIPT"
cp "$SCRIPT_DIR/cm" "$CM_SCRIPT"
chmod +x "$CM_SCRIPT"

echo "Writing cm wrapper to $CM_WRAPPER"
rm -f "$CM_WRAPPER"
cat > "$CM_WRAPPER" <<'EOF'
#!/bin/sh
SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
exec "$SCRIPT_DIR/.cm-venv/bin/python" "$SCRIPT_DIR/cm.py" "$@"
EOF
chmod +x "$CM_WRAPPER"

# Inject version from git
if command -v git >/dev/null 2>&1 && git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    CM_VERSION=$(git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null)
    if [[ -n "$CM_VERSION" ]]; then
        CM_VERSION_SAFE=$(printf '%s' "$CM_VERSION" | sed 's/[&/\]/\\&/g')
        if [[ "$OSTYPE" == darwin* ]]; then
            sed -i '' "s/^VERSION = \"dev\"$/VERSION = \"$CM_VERSION_SAFE\"/" "$CM_SCRIPT"
        else
            sed -i "s/^VERSION = \"dev\"$/VERSION = \"$CM_VERSION_SAFE\"/" "$CM_SCRIPT"
        fi
        echo "Version: $CM_VERSION"
    fi
fi

remove_old_symlink() {
    local link="$1"

    if [[ -L "$link" ]]; then
        rm "$link"
        echo "Removed old symlink: $link"
    elif [[ -e "$link" ]]; then
        echo "Leaving existing non-symlink path: $link"
    fi
}

remove_old_symlink "$INSTALL_DIR/authorized_keys"
remove_old_symlink "$INSTALL_DIR/workspaces"

echo
echo "Installed successfully!"
echo
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    print_path_guidance "$INSTALL_DIR"
fi
