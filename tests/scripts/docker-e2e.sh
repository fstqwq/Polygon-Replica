#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$REPO_ROOT/docker-compose.e2e.yml"

project_suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}"
export COMPOSE_PROJECT_NAME="polygon-replica-e2e-${project_suffix}"
export POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN="e2e-${project_suffix}-judgehost-token"
export POLYGON_REPLICA_E2E_IMAGE="${POLYGON_REPLICA_E2E_IMAGE:-polygon-replica-e2e:${project_suffix}}"

compose=(docker compose --ansi never --project-name "$COMPOSE_PROJECT_NAME" --file "$COMPOSE_FILE")
image_owned=0

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    "${compose[@]}" ps --all >&2 || true
    "${compose[@]}" logs --no-color \
      tex-smoke bootstrap app mock-judgehost runner >&2 || true
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  if (( image_owned )); then
    docker image rm --force "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

cd -- "$REPO_ROOT"
docker compose version >/dev/null

if [[ "${POLYGON_REPLICA_E2E_IMAGE_PREBUILT:-0}" == "1" ]]; then
  docker image inspect "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null
else
  image_owned=1
  "${compose[@]}" build app
fi

# Compile a real PDF through the production TeX sandbox before starting the
# application. The one-shot container is networkless; the smoke verifies the
# image's TeX formats and bwrap root switch rather than only package presence.
"${compose[@]}" run --rm --no-deps tex-smoke

# Bootstrap has no network at all.  It creates the fresh database, authoring
# workspace, session, and deterministic verification fixture in named volumes.
"${compose[@]}" run --rm --no-deps bootstrap

# Neither service publishes a host port.  Their only network is Compose's
# internal network, so this stack cannot discover or contact a real Judgehost.
"${compose[@]}" up --detach --wait app mock-judgehost
"${compose[@]}" run --rm --no-deps runner

echo "Docker E2E completed successfully (project $COMPOSE_PROJECT_NAME)."
