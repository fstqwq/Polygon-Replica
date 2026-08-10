#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Keep stale testsuite cleanup conservative under concurrent runs.
: "${POLYGON_REPLICA_TESTSUITE_STALE_TTL_SEC:=3600}"
export POLYGON_REPLICA_TESTSUITE_STALE_TTL_SEC

if [[ "$#" -gt 0 ]]; then
  groups=("$@")
elif [[ -n "${POLYGON_REPLICA_TEST_GROUPS:-}" ]]; then
  IFS=',' read -r -a groups <<< "$POLYGON_REPLICA_TEST_GROUPS"
else
  groups=(unit service executor e2e)
fi

for group in "${groups[@]}"; do
  # Each resource contract is enforced in a fresh interpreter. In particular,
  # unit tests must prove that they never load the global runtime config.
  python tests/scripts/run_test_groups.py "$group"
done
