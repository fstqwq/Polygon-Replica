#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

failed=0

# Detect unfinished refactor placeholders in repository source files.
scan_pattern() {
  local pattern="$1"
  local message="$2"
  if command -v rg >/dev/null 2>&1; then
    if rg -n --glob '*.py' "$pattern" app tests scripts; then
      echo "$message" >&2
      failed=1
    fi
  elif grep -RInE --include='*.py' "$pattern" app tests scripts; then
    echo "$message" >&2
    failed=1
  fi
}

scan_pattern \
  '^[[:space:]]*#[[:space:]]*Split module placeholder for refactor task' \
  'unfinished split-module placeholder marker detected'

scan_pattern \
  '^[[:space:]]*#[[:space:]].*moved?[[:space:]]+here[[:space:]]+in[[:space:]]+task[[:space:]]+[0-9]+\.[0-9]+' \
  'unfinished task-targeted migration placeholder marker detected'

if ! scripts/check-forwarding-shims.sh; then
  echo "forwarding shim module detected" >&2
  failed=1
fi

if ! scripts/check-cross-package-private-imports.sh; then
  echo "cross-package private import policy violation detected" >&2
  failed=1
fi

exit "$failed"
