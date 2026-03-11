#!/usr/bin/env bash
set -euo pipefail

if ! command -v uvicorn >/dev/null 2>&1 && [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export POLYGONLIKE_DB=${POLYGONLIKE_DB:-./var/polygonlike.db}
export POLYGONLIKE_BARE_ROOT=${POLYGONLIKE_BARE_ROOT:-./var/srv/git}
export POLYGONLIKE_WORKSPACE_ROOT=${POLYGONLIKE_WORKSPACE_ROOT:-./var/srv/workspaces}
export POLYGONLIKE_RUN_ROOT=${POLYGONLIKE_RUN_ROOT:-./var/srv/runs}
export POLYGONLIKE_ARTIFACTS_ROOT=${POLYGONLIKE_ARTIFACTS_ROOT:-./var/lib/polygonlike/artifacts}
export POLYGONLIKE_CACHE_ROOT=${POLYGONLIKE_CACHE_ROOT:-./var/cache/polygonlike}
export POLYGONLIKE_AUTH_COOKIE_SECURE=1

HOST=${POLYGONLIKE_HOST:-127.0.0.1}
PORT=${POLYGONLIKE_PORT:-8000}
RELOAD=${POLYGONLIKE_DEV_RELOAD:-0}
UVICORN_GRACEFUL_TIMEOUT_SEC=${POLYGONLIKE_UVICORN_GRACEFUL_TIMEOUT_SEC:-20}
KEEPALIVE_TIMEOUT_SEC=${POLYGONLIKE_KEEPALIVE_TIMEOUT_SEC:-2}
SHUTDOWN_TIMEOUT_SEC=${POLYGONLIKE_SHUTDOWN_TIMEOUT_SEC:-30}
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

mkdir -p ./var
mkdir -p ./var/tls
TLS_KEY=${POLYGONLIKE_TLS_KEY_PATH:-./var/tls/dev-localhost.key}
TLS_CERT=${POLYGONLIKE_TLS_CERT_PATH:-./var/tls/dev-localhost.crt}

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
