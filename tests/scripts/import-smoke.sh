#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON:-.venv/bin/python}"

"$PYTHON_BIN" - <<'PY'
import importlib

modules = [
    "app.service.statement",
    "app.service.statement.constant",
    "app.service.statement.render",
    "app.service.judgehost.api",
    "app.impl.auth.api",
    "app.impl.auth.shared",
    "app.impl.root.api",
    "app.impl.preview.preview",
    "app.impl.problem.file",
    "app.impl.run_export.run",
    "app.impl.tests_spec.test_spec",
    "app.impl.workspace.context_ui",
    "app.route.problem_route",
    "app.route.tests_route",
    "app.route.run_export_route",
    "app.route.contest_route",
]

for name in modules:
    importlib.import_module(name)

print("import smoke ok")
PY
