#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$REPO_ROOT/docker-compose.e2e.yml"
MOCK_COMPOSE_FILE="$REPO_ROOT/docker-compose.e2e-mock.yml"

project_suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}"
export COMPOSE_PROJECT_NAME="polygon-replica-e2e-mock-${project_suffix}"
export POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN="e2e-${project_suffix}-judgehost-token"
export POLYGON_REPLICA_E2E_ADMIN_PASSWORD="e2e-${project_suffix}-Password-9"
export POLYGON_REPLICA_E2E_IMAGE="${POLYGON_REPLICA_E2E_IMAGE:-polygon-replica-e2e-mock:${project_suffix}}"

compose=(
  docker compose
  --ansi never
  --project-name "$COMPOSE_PROJECT_NAME"
  --file "$COMPOSE_FILE"
  --file "$MOCK_COMPOSE_FILE"
)

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    "${compose[@]}" ps --all >&2 || true
    "${compose[@]}" logs --no-color \
      app mock-judgehost e2e-mock >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm --force "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

cd -- "$REPO_ROOT"
docker compose version >/dev/null

if [[ "${POLYGON_REPLICA_E2E_IMAGE_PREBUILT:-0}" == "1" ]]; then
  docker image inspect "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null
else
  "${compose[@]}" build app
fi

# Start the unmodified production entrypoint against entirely fresh volumes.
"${compose[@]}" up --detach --wait app

# All durable business writes happen through public HTTP before the mock is
# allowed to register: setup, Judgehost runtime config, problem creation, files.
"${compose[@]}" run --rm --no-deps e2e-mock prepare

"${compose[@]}" up --detach --wait mock-judgehost
"${compose[@]}" run --rm --no-deps e2e-mock verify-commit

echo "Mock Judgehost E2E completed successfully (project $COMPOSE_PROJECT_NAME)."
