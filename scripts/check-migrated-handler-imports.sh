#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

failed=0

# Disallow monolithic namespace imports in migrated route/runtime paths.
# Expected style: import concrete modules/functions directly.
if grep -RInE --include='*.py' '^[[:space:]]*from[[:space:]]+app\.impl[[:space:]]+import[[:space:]]+(problem_editor|build_preview|run_export|contests|auth)[[:space:]]+as[[:space:]]+handlers' app/routes app/impl/config.py app/main.py; then
  echo "forbidden monolithic namespace import detected for migrated impl domains" >&2
  failed=1
fi

exit "$failed"
