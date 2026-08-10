#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

BOUNDARIES="$ROOT/import-policy/import-boundaries.json"

if [[ "${IMPORT_POLICY_CHANGED_ONLY:-0}" == "1" ]]; then
  BASE_REF="${IMPORT_POLICY_BASE_REF:-origin/main}"
  "$PYTHON_BIN" tests/scripts/import_policy.py \
    --boundaries "$BOUNDARIES" \
    check \
    --changed-only \
    --base-ref "$BASE_REF"
else
  "$PYTHON_BIN" tests/scripts/import_policy.py --boundaries "$BOUNDARIES" check
fi
