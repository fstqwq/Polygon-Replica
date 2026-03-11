#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python}"

"$PYTHON_BIN" - <<'PY'
import importlib

modules = [
    "app.service.statement",
    "app.service.statement.constant",
    "app.service.statement.render",
    "app.service.run.api",
    "app.service.judgehost.api",
    "app.service.build.api",
    "app.impl.workspace.api",
    "app.impl.problem_editor.api",
    "app.impl.build_preview.api",
    "app.impl.run_export.api",
    "app.impl.contests.api",
    "app.impl.auth.api",
]

for name in modules:
    importlib.import_module(name)

print("import smoke ok")
PY