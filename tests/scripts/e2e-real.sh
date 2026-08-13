#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$REPO_ROOT/tests/docker_e2e/docker-compose.e2e.yml"
REAL_COMPOSE_FILE="$REPO_ROOT/tests/docker_e2e/docker-compose.e2e-real.yml"

run_suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}"
export POLYGON_REPLICA_E2E_IMAGE="${POLYGON_REPLICA_E2E_IMAGE:-polygon-replica-e2e-real:${run_suffix}}"
export POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT="${POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT:-$REPO_ROOT/.e2e/polygon-skills}"
export POLYGON_REPLICA_E2E_SKILLS_COMMIT
export POLYGON_REPLICA_E2E_JUDGEHOST_IMAGE="domjudge/judgehost:9.0.0"
export POLYGON_REPLICA_E2E_JUDGEHOST_CONTAINER_HOSTNAME="judgedaemon-0"
export POLYGON_REPLICA_E2E_JUDGEHOST_HOSTNAME="judgedaemon-0-0"
export POLYGON_REPLICA_E2E_JUDGEHOST_DAEMON_ID="0"
export POLYGON_REPLICA_E2E_JUDGEHOST_RUN_UID="60720"
export POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN="e2e-${run_suffix}-judgehost-token"
export POLYGON_REPLICA_E2E_ADMIN_PASSWORD="e2e-${run_suffix}-Password-9"
export POLYGON_REPLICA_E2E_VARIANT="domjudge-9.0.0"
export POLYGON_REPLICA_E2E_PRODUCT_TAIL="1"

if [[ ! -f "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT/polygon-agent-cli/scripts/polygon_agent.py" ]]; then
  echo "Polygon-Skills checkout is missing: $POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" >&2
  exit 1
fi
POLYGON_REPLICA_E2E_SKILLS_COMMIT=$(git -C "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" rev-parse HEAD)
echo "Testing Polygon-Skills commit $POLYGON_REPLICA_E2E_SKILLS_COMMIT"

project_name="polygon-replica-e2e-real-${run_suffix}"
output_root="$REPO_ROOT/.e2e/e2e-real"
controller_log="$output_root/controller.log"
image_owned=0

compose() {
  docker compose --ansi never \
    --project-name "$project_name" \
    --file "$COMPOSE_FILE" \
    --file "$REAL_COMPOSE_FILE" \
    "$@"
}

collect_logs() {
  mkdir -p "$output_root"
  compose ps --all >"$output_root/compose-ps.txt" 2>&1 || true
  for service in app judgehost e2e-real; do
    compose logs --no-color "$service" >"$output_root/${service}.log" 2>&1 || true
  done
}

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    collect_logs
    cat "$controller_log" >&2 2>/dev/null || true
    compose ps --all >&2 || true
    compose logs --no-color app judgehost e2e-real >&2 || true
  fi
  timeout 60s compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  if (( image_owned )); then
    docker image rm --force "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

cd -- "$REPO_ROOT"
rm -rf -- "$output_root"
mkdir -p "$output_root"
docker compose version >/dev/null

if [[ "${POLYGON_REPLICA_E2E_IMAGE_PREBUILT:-0}" == "1" ]]; then
  docker image inspect "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null
else
  image_owned=1
  compose build app
fi

docker pull "$POLYGON_REPLICA_E2E_JUDGEHOST_IMAGE"
docker image inspect --format '{{index .RepoDigests 0}}' \
  "$POLYGON_REPLICA_E2E_JUDGEHOST_IMAGE"

compose up --detach --wait app
compose run --rm --no-deps e2e-real prepare | tee "$controller_log"
compose up --detach judgehost
compose run --rm --no-deps e2e-real verify | tee -a "$controller_log"
compose run --rm --no-deps e2e-real restart | tee -a "$controller_log"

echo "Real product E2E completed with domjudge/judgehost:9.0.0."
