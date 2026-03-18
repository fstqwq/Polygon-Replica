#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

BASE_REF="${1:-origin/main}"
IMPORT_POLICY_CHANGED_ONLY=1 IMPORT_POLICY_BASE_REF="$BASE_REF" bash tests/scripts/check-import-policy.sh
