#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 5 ]]; then
  echo "usage: render_systemd_unit.sh TEMPLATE OUTPUT USER GROUP REPO_ROOT" >&2
  exit 2
fi

TEMPLATE_PATH="$1"
OUTPUT_PATH="$2"
RUNTIME_USER="$3"
RUNTIME_GROUP="$4"
REPO_ROOT="$5"

if [[ "$REPO_ROOT" == *$'\n'* || "$REPO_ROOT" == *$'\r'* ]]; then
  echo "repository path contains an unsupported newline" >&2
  exit 2
fi

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//%/%%}"
  printf '"%s"' "$value"
}

CONTENT="$(<"$TEMPLATE_PATH")"
CONTENT="${CONTENT//@RUNTIME_USER@/$RUNTIME_USER}"
CONTENT="${CONTENT//@RUNTIME_GROUP@/$RUNTIME_GROUP}"
CONTENT="${CONTENT//@WORKING_DIRECTORY@/$(systemd_quote "$REPO_ROOT")}"
CONTENT="${CONTENT//@UVICORN_EXECUTABLE@/$(systemd_quote "$REPO_ROOT/.venv/bin/uvicorn")}"
printf '%s\n' "$CONTENT" >"$OUTPUT_PATH"
