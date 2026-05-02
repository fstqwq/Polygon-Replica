#!/usr/bin/env bash
set -euo pipefail

# Ensure runtime subdirectories exist on first boot of a fresh volume.
mkdir -p \
  "$(dirname "$POLYGON_REPLICA_DB")" \
  "$POLYGON_REPLICA_BARE_ROOT" \
  "$POLYGON_REPLICA_WORKSPACE_ROOT" \
  "$POLYGON_REPLICA_ARTIFACTS_ROOT" \
  "$POLYGON_REPLICA_CACHE_ROOT"

# Quick bubblewrap probe so misconfigured hosts fail loudly rather than
# crashing later inside a verification job.
if ! bwrap --die-with-parent --new-session --ro-bind / / --chdir / -- /bin/sh -lc 'true' >/dev/null 2>&1; then
  echo "bubblewrap probe failed inside the container." >&2
  echo "Set on the host: kernel.unprivileged_userns_clone=1," >&2
  echo "user.max_user_namespaces=1048576, kernel.apparmor_restrict_unprivileged_userns=0," >&2
  echo "and run the container with seccomp=unconfined and apparmor=unconfined." >&2
  exit 1
fi

HOST=${POLYGON_REPLICA_HOST:-0.0.0.0}
PORT=${POLYGON_REPLICA_PORT:-8001}

exec uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --proxy-headers \
  --forwarded-allow-ips='*'
