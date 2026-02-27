#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

mapfile -t PY_FILES < <(rg --files app tests scripts | rg '\.py$')

if [[ ${#PY_FILES[@]} -eq 0 ]]; then
  echo "No python files under app/ tests/ or scripts/."
  exit 0
fi

echo "[1/4] Syntax check (py_compile)"
python -m py_compile "${PY_FILES[@]}"

echo "[2/4] Lint check (pyflakes)"
python -m pyflakes "${PY_FILES[@]}"

echo "[3/4] Dead code check (vulture, confidence=60)"
python -m vulture app tests --min-confidence 60

echo "[4/4] Unit tests"
python -m unittest discover -s tests -p 'test_*.py' -v
