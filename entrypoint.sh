#!/bin/bash
set -e

echo "===================================="
echo "CM Container Starting"
echo "===================================="

# Add local binaries to PATH for SSH sessions
echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/me/.bashrc

AUTHORIZED_KEYS_SRC="/tmp/cm_authorized_keys"
AUTHORIZED_KEYS_DST="/home/me/.ssh/authorized_keys"
SSH_DIR="/home/me/.ssh"

if [ ! -f "$AUTHORIZED_KEYS_SRC" ]; then
    echo "Error: authorized_keys source not found at $AUTHORIZED_KEYS_SRC" >&2
    echo "Mount a non-empty authorized_keys file from the host." >&2
    exit 1
fi

if [ ! -s "$AUTHORIZED_KEYS_SRC" ]; then
    echo "Error: authorized_keys source is empty at $AUTHORIZED_KEYS_SRC" >&2
    echo "Add at least one public key before starting the container." >&2
    exit 1
fi

install -d -o me -g me -m 700 "$SSH_DIR"
install -o me -g me -m 600 "$AUTHORIZED_KEYS_SRC" "$AUTHORIZED_KEYS_DST"

# Start SSH daemon
/usr/sbin/sshd -D &

# Keep container running
exec tail -f /dev/null
