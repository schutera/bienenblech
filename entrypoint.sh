#!/bin/sh
# Boot-time self-heal for the /app/data bind mount, then drop to the app user.
#
# The app runs as the unprivileged `bienenblech` user (uid 10001), but the data
# directory is a HOST bind mount: any root-run host tool that writes into it
# (a restore from a backup zip, a manual copy) plants root-owned files the app
# cannot create siblings of or overwrite — which surfaces later as a 500 on some
# unrelated save (PermissionError on mkdir/open). The build-time chown in the
# Dockerfile cannot fix this: it runs before the mount exists.
#
# So the container starts as root just long enough to re-own anything in
# /app/data that drifted, then execs the real command as `bienenblech` with root
# privileges gone. The find only touches wrong-owned files, so a clean boot
# does no chown work at all.
set -eu

if [ "$(id -u)" = "0" ]; then
    if [ -d /app/data ]; then
        find /app/data ! -user bienenblech -exec chown bienenblech:bienenblech {} + || \
            echo "[bienenblech.entrypoint] WARNING: could not re-own some of /app/data" >&2
    fi
    exec setpriv --reuid=bienenblech --regid=bienenblech --init-groups "$@"
fi

# Already unprivileged (e.g. compose overrides `user:`): just run.
exec "$@"
