#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIST="$ROOT/migration-gates/enforced-legacy-modules.txt"

if [ ! -f "$LIST" ]; then
  echo "skipping migrated service import gate; missing list: $LIST" >&2
  exit 0
fi

failed=0
while IFS= read -r module; do
  module="${module%%#*}"
  module="${module//[[:space:]]/}"
  [ -z "$module" ] && continue
  if grep -RInE --include='*.py' "(^|[[:space:]])from[[:space:]]+${module}[[:space:]]+import|(^|[[:space:]])import[[:space:]]+${module}([[:space:]]|$)" "$ROOT/app" "$ROOT/tests"; then
    echo "legacy import detected: $module" >&2
    failed=1
  fi
done < "$LIST"

exit "$failed"
