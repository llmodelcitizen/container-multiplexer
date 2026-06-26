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
WORKSPACE_DIR="/home/me/workspace"

if [ -n "${CM_HOST_UID:-}" ] || [ -n "${CM_HOST_GID:-}" ]; then
    if ! [[ "${CM_HOST_UID:-}" =~ ^[1-9][0-9]*$ ]] || ! [[ "${CM_HOST_GID:-}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: CM_HOST_UID and CM_HOST_GID must be positive integers." >&2
        exit 1
    fi

    current_uid="$(id -u me)"
    current_gid="$(id -g me)"

    if [ "$CM_HOST_GID" != "$current_gid" ]; then
        target_group="$(getent group "$CM_HOST_GID" | cut -d: -f1 || true)"
        if [ -n "$target_group" ] && [ "$target_group" != "me" ]; then
            usermod -g "$target_group" me
        else
            groupmod -g "$CM_HOST_GID" me
        fi
    fi

    if [ "$CM_HOST_UID" != "$current_uid" ]; then
        target_user="$(getent passwd "$CM_HOST_UID" | cut -d: -f1 || true)"
        if [ -n "$target_user" ] && [ "$target_user" != "me" ]; then
            echo "Error: cannot set user me to UID $CM_HOST_UID; UID is already used by $target_user." >&2
            exit 1
        fi
        usermod -u "$CM_HOST_UID" me
    fi

    chown me:"$(id -gn me)" /home/me
    if [ -f /home/me/.bashrc ]; then
        chown me:"$(id -gn me)" /home/me/.bashrc
    fi
    if [ -d "$SSH_DIR" ]; then
        chown -R me:"$(id -gn me)" "$SSH_DIR"
    fi
fi

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

ME_GROUP="$(id -gn me)"
install -d -o me -g "$ME_GROUP" -m 700 "$SSH_DIR"
install -o me -g "$ME_GROUP" -m 600 "$AUTHORIZED_KEYS_SRC" "$AUTHORIZED_KEYS_DST"

if [ -n "${CM_HOST_UID:-}" ] && [ -d "$WORKSPACE_DIR" ] && ! sudo -u me test -w "$WORKSPACE_DIR"; then
    echo "Error: workspace is not writable by user me: $WORKSPACE_DIR" >&2
    echo "Check host ownership of the bind-mounted workspace." >&2
    exit 1
fi

# Start SSH daemon as the container foreground process
exec /usr/sbin/sshd -D -e
