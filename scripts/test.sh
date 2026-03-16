#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Keep stale testsuite cleanup conservative under concurrent runs.
: "${POLYGONLIKE_TESTSUITE_STALE_TTL_SEC:=3600}"
export POLYGONLIKE_TESTSUITE_STALE_TTL_SEC

if command -v rg >/dev/null 2>&1; then
  mapfile -t PY_FILES < <(rg --files app tests scripts | rg '\.py$')
else
  mapfile -t PY_FILES < <(find app tests scripts -type f -name '*.py' -print)
fi

if [[ ${#PY_FILES[@]} -eq 0 ]]; then
  echo "No python files under app/ tests/ or scripts/."
  exit 0
fi

echo "[1/5] Syntax check (py_compile)"
python -m py_compile "${PY_FILES[@]}"

echo "[2/5] Lint check (pyflakes)"
python -m pyflakes "${PY_FILES[@]}"

echo "[3/5] Dead code check (vulture, confidence=60)"
python -m vulture app tests --min-confidence 60

echo "[4/5] Import architecture policy checks"
bash scripts/check-import-policy.sh
bash scripts/check-refactor-placeholders.sh

echo "[5/5] Unit tests"
: "${POLYGONLIKE_INCLUDE_SLOW_TESTS:=0}"
if [[ "$POLYGONLIKE_INCLUDE_SLOW_TESTS" == "1" ]]; then
  echo "Running full unittest suite (including slow ui integration tests)."
  mapfile -t TEST_FILES < <(find tests -maxdepth 1 -type f -name 'test_*.py' -print | sort)
else
  echo "Running fast unittest suite (skipping slow ui integration tests: tests/test_ui_*.py)."
  echo "Set POLYGONLIKE_INCLUDE_SLOW_TESTS=1 to include them."
  mapfile -t TEST_FILES < <(find tests -maxdepth 1 -type f -name 'test_*.py' ! -name 'test_ui_*.py' -print | sort)
fi

if [[ ${#TEST_FILES[@]} -eq 0 ]]; then
  echo "No test files selected."
  exit 0
fi

TEST_MODULES=()
for file in "${TEST_FILES[@]}"; do
  module="${file%.py}"
  module="${module//\//.}"
  TEST_MODULES+=("$module")
done

python -m unittest -v "${TEST_MODULES[@]}"
