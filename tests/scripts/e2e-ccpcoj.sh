#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
work=$(mktemp -d)
export CCPCOJ_FIXTURE_ZIP="$work/synthetic.zip"
project="ccpcoj-e2e-$(basename "$work" | tr '[:upper:].' '[:lower:]-')"
compose=(docker compose -p "$project" -f tests/docker_e2e/docker-compose.ccpcoj.yml)
cleanup() {
    "${compose[@]}" down -v --remove-orphans
    rm -f -- "$CCPCOJ_FIXTURE_ZIP"
    rmdir -- "$work"
}
trap cleanup EXIT
python -m tests.docker_e2e.ccpcoj_fixture "$CCPCOJ_FIXTURE_ZIP"
"${compose[@]}" run --rm importer
"${compose[@]}" run --rm --no-deps checker
