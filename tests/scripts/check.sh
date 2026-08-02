#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

if command -v rg >/dev/null 2>&1; then
  mapfile -t PY_FILES < <(rg --files app tests scripts | rg '\.py$')
else
  mapfile -t PY_FILES < <(find app tests scripts -type f -name '*.py' -print)
fi

echo "[1/4] Syntax check (py_compile)"
python -m py_compile "${PY_FILES[@]}"

echo "[2/4] Lint check (pyflakes)"
python -m pyflakes "${PY_FILES[@]}"

echo "[3/4] Dead code check (vulture, confidence=70)"
python -m vulture app tests --min-confidence 70

echo "[4/4] Import architecture policy checks"
bash tests/scripts/check-import-policy.sh
bash tests/scripts/check-refactor-placeholders.sh
