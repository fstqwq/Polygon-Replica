#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
COMPOSE_FILE="$REPO_ROOT/docker-compose.e2e.yml"
REAL_COMPOSE_FILE="$REPO_ROOT/docker-compose.e2e-real.yml"

run_suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}"
export POLYGON_REPLICA_E2E_IMAGE="${POLYGON_REPLICA_E2E_IMAGE:-polygon-replica-e2e-real:${run_suffix}}"
export POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT="${POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT:-$REPO_ROOT/.e2e/polygon-skills}"

if [[ ! -f "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT/polygon-agent-cli/scripts/polygon_agent.py" ]]; then
  echo "Polygon-Skills checkout is missing: $POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" >&2
  exit 1
fi
export POLYGON_REPLICA_E2E_SKILLS_COMMIT
POLYGON_REPLICA_E2E_SKILLS_COMMIT=$(git -C "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" rev-parse HEAD)
echo "Testing Polygon-Skills commit $POLYGON_REPLICA_E2E_SKILLS_COMMIT"

image_owned=0
pids=()
variants=(stable bleeding)

compose() {
  local variant="$1"
  shift
  docker compose --ansi never \
    --project-name "polygon-replica-e2e-real-${variant}-${run_suffix}" \
    --file "$COMPOSE_FILE" \
    --file "$REAL_COMPOSE_FILE" \
    "$@"
}

variant_env() {
  local variant="$1"
  export POLYGON_REPLICA_E2E_VARIANT="$variant"
  export POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN="e2e-${run_suffix}-${variant}-judgehost-token"
  export POLYGON_REPLICA_E2E_ADMIN_PASSWORD="e2e-${run_suffix}-${variant}-Password-9"
  if [[ "$variant" == "stable" ]]; then
    export POLYGON_REPLICA_E2E_JUDGEHOST_IMAGE="domjudge/judgehost:9.0.0"
    export POLYGON_REPLICA_E2E_JUDGEHOST_CONTAINER_HOSTNAME="judgedaemon-0"
    export POLYGON_REPLICA_E2E_JUDGEHOST_HOSTNAME="judgedaemon-0-0"
    export POLYGON_REPLICA_E2E_JUDGEHOST_DAEMON_ID="0"
    export POLYGON_REPLICA_E2E_JUDGEHOST_RUN_UID="60720"
    export POLYGON_REPLICA_E2E_PRODUCT_TAIL="0"
  else
    export POLYGON_REPLICA_E2E_JUDGEHOST_IMAGE="domjudge/judgehost:bleeding"
    export POLYGON_REPLICA_E2E_JUDGEHOST_CONTAINER_HOSTNAME="judgedaemon-1"
    export POLYGON_REPLICA_E2E_JUDGEHOST_HOSTNAME="judgedaemon-1-1"
    export POLYGON_REPLICA_E2E_JUDGEHOST_DAEMON_ID="1"
    export POLYGON_REPLICA_E2E_JUDGEHOST_RUN_UID="60721"
    export POLYGON_REPLICA_E2E_PRODUCT_TAIL="1"
  fi
}

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    for variant in "${variants[@]}"; do
      variant_env "$variant"
      echo "--- $variant stack ---" >&2
      compose "$variant" ps --all >&2 || true
      compose "$variant" logs --no-color app judgehost e2e-real >&2 || true
    done
  fi
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  for variant in "${variants[@]}"; do
    variant_env "$variant"
    compose "$variant" down --volumes --remove-orphans >/dev/null 2>&1 || true
  done
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
  variant_env stable
  compose stable build app
fi

for image in domjudge/judgehost:9.0.0 domjudge/judgehost:bleeding; do
  docker pull "$image" &
  pids+=("$!")
done
pull_status=0
for pid in "${pids[@]}"; do
  wait "$pid" || pull_status=1
done
pids=()
if (( pull_status )); then
  echo "Failed to pull one or more official Judgehost images." >&2
  exit 1
fi
for image in domjudge/judgehost:9.0.0 domjudge/judgehost:bleeding; do
  docker image inspect --format '{{index .RepoDigests 0}}' "$image"
done
stable_image_id=$(docker image inspect --format '{{.Id}}' domjudge/judgehost:9.0.0)
bleeding_image_id=$(docker image inspect --format '{{.Id}}' domjudge/judgehost:bleeding)
if [[ "$stable_image_id" == "$bleeding_image_id" ]]; then
  echo "Stable and bleeding Judgehost tags resolved to the same image." >&2
  exit 1
fi

run_variant() {
  local variant="$1"
  variant_env "$variant"
  compose "$variant" up --detach --wait app
  compose "$variant" run --rm --no-deps e2e-real prepare
  compose "$variant" up --detach judgehost
  compose "$variant" run --rm --no-deps e2e-real verify
  if [[ "$POLYGON_REPLICA_E2E_PRODUCT_TAIL" == "1" ]]; then
    compose "$variant" run --rm --no-deps e2e-real restart
  fi
}

mkdir -p "$REPO_ROOT/.e2e"
stable_log="$REPO_ROOT/.e2e/e2e-real-stable.log"
bleeding_log="$REPO_ROOT/.e2e/e2e-real-bleeding.log"
(run_variant stable >"$stable_log" 2>&1) &
pids+=("$!")
(run_variant bleeding >"$bleeding_log" 2>&1) &
pids+=("$!")
run_status=0
for pid in "${pids[@]}"; do
  wait "$pid" || run_status=1
done
pids=()
for log_path in "$stable_log" "$bleeding_log"; do
  cat "$log_path"
done
if (( run_status )); then
  exit 1
fi

echo "Real Judgehost E2E completed for 9.0.0 and bleeding."
