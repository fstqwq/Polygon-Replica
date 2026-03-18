#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

"$PYTHON_BIN" - <<'PY'
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path("app")
violations: list[str] = []

for path in ROOT.rglob("*.py"):
    if path.name == "__init__.py":
        continue
    source = path.read_text(encoding="utf-8-sig")
    try:
        module = ast.parse(source, filename=str(path))
    except SyntaxError:
        continue

    body = list(module.body)
    if body and isinstance(body[0], ast.Expr):
        value = body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            body = body[1:]
    if len(body) > 8:
        continue

    imports: set[str] = set()
    for node in body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports.add(alias.asname or alias.name)

    func_defs = [node for node in body if isinstance(node, ast.FunctionDef)]
    if len(func_defs) != 1:
        continue

    fn = func_defs[0]
    if len(fn.body) != 1 or not isinstance(fn.body[0], ast.Return):
        continue
    ret = fn.body[0].value
    if not isinstance(ret, ast.Call) or not isinstance(ret.func, ast.Name):
        continue
    target = ret.func.id
    if not target.startswith("_"):
        continue
    if target not in imports:
        continue

    violations.append(f"{path.as_posix()}:{fn.lineno} forwarding shim `{fn.name} -> {target}`")

if violations:
    print("\n".join(violations))
    sys.exit(1)
PY
