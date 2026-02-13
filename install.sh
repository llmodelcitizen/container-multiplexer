#!/bin/bash
# Install cm to a directory in PATH

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$HOME/.local/bin"

echo "CM Installer"
echo "============"
echo
echo "This will:"
echo "  1. Copy 'cm' to your chosen directory"
echo "  2. Create symlinks for 'workspaces/' (and 'authorized_keys' if present)"
echo
read -p "Install directory [$DEFAULT_INSTALL_DIR]: " INSTALL_DIR
INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"

# Expand ~
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

# Create install directory if needed
if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "Creating $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
fi

# Copy cm script
echo "Copying cm to $INSTALL_DIR/"
rm -f "$INSTALL_DIR/cm"
cp "$SCRIPT_DIR/cm" "$INSTALL_DIR/cm"
chmod +x "$INSTALL_DIR/cm"

# Inject version from git
if command -v git >/dev/null 2>&1 && git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    CM_VERSION=$(git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null)
    if [[ -n "$CM_VERSION" ]]; then
        sed -i "s/^VERSION = \"dev\"$/VERSION = \"$CM_VERSION\"/" "$INSTALL_DIR/cm"
        echo "Version: $CM_VERSION"
    fi
fi

# Helper to safely create symlink (only removes existing symlinks, never directories)
safe_symlink() {
    local target="$1"
    local link="$2"
    local name="$(basename "$link")"

    if [[ -L "$link" ]]; then
        rm "$link"
    elif [[ -e "$link" ]]; then
        echo "Error: $link exists and is not a symlink. Remove it manually to proceed."
        exit 1
    fi
    ln -s "$target" "$link"
    echo "Created symlink: $link -> $target"
}

if [[ -f "$SCRIPT_DIR/authorized_keys" ]]; then
    safe_symlink "$SCRIPT_DIR/authorized_keys" "$INSTALL_DIR/authorized_keys"
else
    echo "No project-root authorized_keys found; will use ~/.ssh/authorized_keys"
fi
safe_symlink "$SCRIPT_DIR/workspaces" "$INSTALL_DIR/workspaces"

echo
echo "Installed successfully!"
echo
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo "Note: $INSTALL_DIR is not in your PATH."
    echo "Add it with:"
    echo "  echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.bashrc"
fi
