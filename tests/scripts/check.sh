#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

PYTHON_BIN="${PYTHON:-python}"
export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"

if command -v rg >/dev/null 2>&1; then
  mapfile -t PY_FILES < <(rg --files app tests scripts | rg '\.py$')
else
  mapfile -t PY_FILES < <(find app tests scripts -type f -name '*.py' -print)
fi

echo "[1/7] Application lint check (pylint)"
"$PYTHON_BIN" -m pylint app

echo "[2/7] Syntax check (py_compile)"
"$PYTHON_BIN" -m py_compile "${PY_FILES[@]}"

echo "[3/7] Lint check (pyflakes)"
"$PYTHON_BIN" -m pyflakes "${PY_FILES[@]}"

echo "[4/7] Type check (mypy, Linux target)"
"$PYTHON_BIN" -m mypy --platform linux app

echo "[5/7] Dead code check (vulture, confidence=70)"
"$PYTHON_BIN" -m vulture app tests --min-confidence 70

echo "[6/7] Test resource assignment check"
"$PYTHON_BIN" tests/scripts/run_test_groups.py --check-manifest

echo "[7/7] Import architecture policy checks"
bash tests/scripts/check-import-policy.sh
bash tests/scripts/check-refactor-placeholders.sh
