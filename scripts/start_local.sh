#!/usr/bin/env bash
set -euo pipefail

if ! command -v uvicorn >/dev/null 2>&1 && [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export POLYGON_REPLICA_DB=${POLYGON_REPLICA_DB:-/var/lib/polygon-replica/metadata.db}
export POLYGON_REPLICA_BARE_ROOT=${POLYGON_REPLICA_BARE_ROOT:-/srv/polygon-replica/git}
export POLYGON_REPLICA_WORKSPACE_ROOT=${POLYGON_REPLICA_WORKSPACE_ROOT:-/srv/polygon-replica/workspaces}
export POLYGON_REPLICA_ARTIFACTS_ROOT=${POLYGON_REPLICA_ARTIFACTS_ROOT:-/srv/polygon-replica/export}
export POLYGON_REPLICA_CACHE_ROOT=${POLYGON_REPLICA_CACHE_ROOT:-/tmp/polygon-replica}
export POLYGON_REPLICA_AUTH_COOKIE_SECURE=1

HOST=${POLYGON_REPLICA_HOST:-127.0.0.1}
PORT=${POLYGON_REPLICA_PORT:-8000}
RELOAD=${POLYGON_REPLICA_DEV_RELOAD:-0}
UVICORN_GRACEFUL_TIMEOUT_SEC=${POLYGON_REPLICA_UVICORN_GRACEFUL_TIMEOUT_SEC:-20}
KEEPALIVE_TIMEOUT_SEC=${POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC:-2}
SHUTDOWN_TIMEOUT_SEC=${POLYGON_REPLICA_SHUTDOWN_TIMEOUT_SEC:-30}
SERVER_PID=""
STOPPING=0

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing hard dependency: $cmd" >&2
    exit 1
  fi
}

require_cmd git
require_cmd python3
require_cmd pdflatex
require_cmd openssl
require_cmd uvicorn
require_cmd setsid

TLS_KEY=${POLYGON_REPLICA_TLS_KEY_PATH:-/var/lib/polygon-replica/tls/dev-localhost.key}
TLS_CERT=${POLYGON_REPLICA_TLS_CERT_PATH:-/var/lib/polygon-replica/tls/dev-localhost.crt}
mkdir -p \
  "$(dirname "$POLYGON_REPLICA_DB")" \
  "$POLYGON_REPLICA_BARE_ROOT" \
  "$POLYGON_REPLICA_WORKSPACE_ROOT" \
  "$POLYGON_REPLICA_ARTIFACTS_ROOT" \
  "$POLYGON_REPLICA_CACHE_ROOT" \
  "$(dirname "$TLS_KEY")"

if [[ ! -s "$TLS_KEY" || ! -s "$TLS_CERT" ]]; then
  openssl req \
    -x509 \
    -nodes \
    -newkey rsa:2048 \
    -keyout "$TLS_KEY" \
    -out "$TLS_CERT" \
    -days 365 \
    -subj "/CN=127.0.0.1" \
    -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" >/dev/null 2>&1 || {
      openssl req \
        -x509 \
        -nodes \
        -newkey rsa:2048 \
        -keyout "$TLS_KEY" \
        -out "$TLS_CERT" \
        -days 365 \
        -subj "/CN=127.0.0.1" >/dev/null 2>&1
    }
fi

stop_server() {
  if [[ "${STOPPING}" -eq 1 ]]; then
    return
  fi
  STOPPING=1
  if [[ -z "${SERVER_PID}" ]]; then
    return
  fi
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    return
  fi

  echo "Stopping server (graceful)..."
  kill -TERM "-${SERVER_PID}" >/dev/null 2>&1 || kill -TERM "${SERVER_PID}" >/dev/null 2>&1 || true
  local deadline=$((SECONDS + SHUTDOWN_TIMEOUT_SEC))
  while kill -0 "${SERVER_PID}" >/dev/null 2>&1; do
    # If the child already exited and is a zombie, treat it as stopped.
    local stat=""
    stat="$(ps -o stat= -p "${SERVER_PID}" 2>/dev/null | tr -d '[:space:]')"
    if [[ "${stat}" == Z* ]]; then
      break
    fi
    if ((SECONDS >= deadline)); then
      echo "Graceful stop timed out; forcing shutdown."
      kill -KILL "-${SERVER_PID}" >/dev/null 2>&1 || kill -KILL "${SERVER_PID}" >/dev/null 2>&1 || true
      break
    fi
    sleep 0.2
  done
}

on_signal() {
  stop_server
  exit 130
}

trap on_signal INT TERM

UVICORN_ARGS=(
  app.main:app
  --host "${HOST}"
  --port "${PORT}"
  --timeout-keep-alive "${KEEPALIVE_TIMEOUT_SEC}"
  --timeout-graceful-shutdown "${UVICORN_GRACEFUL_TIMEOUT_SEC}"
  --ssl-keyfile "${TLS_KEY}"
  --ssl-certfile "${TLS_CERT}"
)

if [[ "${RELOAD}" == "1" ]]; then
  UVICORN_ARGS+=(--reload)
fi

echo "Starting HTTPS server on https://${HOST}:${PORT} (reload=${RELOAD}, keepalive=${KEEPALIVE_TIMEOUT_SEC}s, graceful=${UVICORN_GRACEFUL_TIMEOUT_SEC}s)"
setsid uvicorn "${UVICORN_ARGS[@]}" &
SERVER_PID=$!

set +e
wait "${SERVER_PID}"
RC=$?
set -e

if [[ "${RC}" -eq 130 || "${RC}" -eq 143 ]]; then
  exit 0
fi
exit "${RC}"
