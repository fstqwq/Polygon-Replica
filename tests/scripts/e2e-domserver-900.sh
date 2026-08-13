#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
BASE_COMPOSE="$REPO_ROOT/tests/docker_e2e/docker-compose.e2e.yml"
DOMSERVER_COMPOSE="$REPO_ROOT/tests/docker_e2e/docker-compose.e2e-domserver-900.yml"

run_suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM}"
export POLYGON_REPLICA_E2E_IMAGE="${POLYGON_REPLICA_E2E_IMAGE:-polygon-replica-e2e-domserver:${run_suffix}}"
export POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT="${POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT:-$REPO_ROOT/.e2e/polygon-skills}"
export POLYGON_REPLICA_E2E_SKILLS_COMMIT
export POLYGON_REPLICA_E2E_JUDGEHOST_TOKEN="e2e-${run_suffix}-app-judgehost"
export POLYGON_REPLICA_E2E_ADMIN_PASSWORD="e2e-${run_suffix}-Password-9"
export POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD="pending"
export POLYGON_REPLICA_E2E_DOMJUDGE_JUDGEHOST_PASSWORD="pending"

if [[ ! -f "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT/polygon-agent-cli/scripts/polygon_agent.py" ]]; then
  echo "Polygon-Skills checkout is missing: $POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" >&2
  exit 1
fi
POLYGON_REPLICA_E2E_SKILLS_COMMIT=$(
  git -C "$POLYGON_REPLICA_E2E_SKILLS_HOST_ROOT" rev-parse HEAD
)

project_name="polygon-replica-e2e-domserver-${run_suffix}"
output_root="$REPO_ROOT/.e2e/e2e-domserver-900"
controller_log="$output_root/controller.log"
export POLYGON_REPLICA_E2E_OUTPUT_HOST_ROOT="$output_root/output"
image_owned=0

compose() {
  docker compose --ansi never \
    --project-name "$project_name" \
    --file "$BASE_COMPOSE" \
    --file "$DOMSERVER_COMPOSE" \
    "$@"
}

collect_package_members() {
  package_dir="$POLYGON_REPLICA_E2E_OUTPUT_HOST_ROOT"
  mkdir -p "$package_dir"
  for archive in "$package_dir"/*.zip; do
    [[ -f "$archive" ]] || continue
    unzip -l "$archive" >"$archive.members.txt" 2>&1 || true
  done
}

collect_logs() {
  mkdir -p "$output_root"
  compose ps --all >"$output_root/compose-ps.txt" 2>&1 || true
  for service in \
    app mariadb domserver app-judgehost domserver-judgehost e2e-domserver-900
  do
    compose logs --no-color "$service" >"$output_root/${service}.log" 2>&1 || true
  done
  collect_package_members
}

cleanup() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    collect_logs
    cat "$controller_log" >&2 2>/dev/null || true
    compose ps --all >&2 || true
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
mkdir -p "$POLYGON_REPLICA_E2E_OUTPUT_HOST_ROOT"
chmod 0777 "$POLYGON_REPLICA_E2E_OUTPUT_HOST_ROOT"
docker compose version >/dev/null

if [[ "${POLYGON_REPLICA_E2E_IMAGE_PREBUILT:-0}" == "1" ]]; then
  docker image inspect "$POLYGON_REPLICA_E2E_IMAGE" >/dev/null
else
  image_owned=1
  compose build app
fi

docker pull mariadb:10.11
docker pull domjudge/domserver:9.0.0
docker pull domjudge/judgehost:9.0.0

compose up --detach --wait --wait-timeout 120 app mariadb
compose up --detach domserver
compose run --rm --no-deps e2e-domserver-900 wait-domserver | tee "$controller_log"

export POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD
POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD=$(
  compose exec -T domserver \
    cat /opt/domjudge/domserver/etc/initial_admin_password.secret | tr -d '\r\n'
)
export POLYGON_REPLICA_E2E_DOMJUDGE_JUDGEHOST_PASSWORD
POLYGON_REPLICA_E2E_DOMJUDGE_JUDGEHOST_PASSWORD=$(
  compose exec -T domserver \
    awk '!/^#/ && NF >= 4 {print $4; exit}' \
      /opt/domjudge/domserver/etc/restapi.secret | tr -d '\r\n'
)
if [[ -z "$POLYGON_REPLICA_E2E_DOMSERVER_ADMIN_PASSWORD" || \
      -z "$POLYGON_REPLICA_E2E_DOMJUDGE_JUDGEHOST_PASSWORD" ]]; then
  echo "DOMserver did not publish initial credentials" >&2
  exit 1
fi

compose run --rm --no-deps e2e-domserver-900 prepare | tee -a "$controller_log"
compose up --detach app-judgehost domserver-judgehost
compose run --rm --no-deps e2e-domserver-900 run | tee -a "$controller_log"

echo "DOMserver 9.0.0 projection E2E completed successfully."
