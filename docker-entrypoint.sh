#!/bin/sh
# Run localfy as PUID:PGID (default 1000:1000) instead of root, so files in the
# mounted music and data folders belong to your user on the host.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA_PATH="${DATA_PATH:-/data}"
DOWNLOAD_PATH="${DOWNLOAD_PATH:-/music}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_PATH" "$DOWNLOAD_PATH"
    # Earlier images ran as root, so existing installs have root-owned files the
    # new user couldn't write to. Hand those over. Only root-owned entries are
    # touched, so anything you put in these folders yourself is left alone.
    # A marker records the handover so a big library isn't re-walked on every
    # start; changing PUID/PGID runs it again.
    marker="$DATA_PATH/.localfy-owner"
    if [ "$PUID" != "0" ] && [ "$(cat "$marker" 2>/dev/null)" != "$PUID:$PGID" ]; then
        if find "$DATA_PATH" "$DOWNLOAD_PATH" -xdev -user 0 -exec chown "$PUID:$PGID" {} +; then
            echo "$PUID:$PGID" > "$marker" && chown "$PUID:$PGID" "$marker"
        else
            echo "localfy: warning: couldn't change ownership of some files under $DATA_PATH / $DOWNLOAD_PATH" >&2
        fi
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"
