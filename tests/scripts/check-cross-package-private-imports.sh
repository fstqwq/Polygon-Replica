#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

"$PYTHON_BIN" - <<'PY'
import ast
from pathlib import Path
import sys

violations: list[str] = []

for path in Path("app").rglob("*.py"):
    rel = path.as_posix()
    importer_module = rel[:-3].replace("/", ".")
    importer_pkg = importer_module.rsplit(".", 1)[0] if "." in importer_module else importer_module
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
    except SyntaxError as exc:
        print(f"{rel}:{exc.lineno}: parse error: {exc.msg}")
        sys.exit(1)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level != 0 or not node.module:
            continue
        if not node.module.startswith("app."):
            continue
        provider_mod = str(node.module)
        provider_pkg = provider_mod.rsplit(".", 1)[0] if "." in provider_mod else provider_mod
        if importer_pkg == provider_pkg:
            continue
        if provider_pkg.startswith(f"{importer_pkg}.") or importer_pkg.startswith(f"{provider_pkg}."):
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            if alias.name.startswith("_"):
                violations.append(
                    f"{rel}:{node.lineno}: forbidden cross-package private import "
                    f"`from {provider_mod} import {alias.name}` "
                    f"(importer={importer_pkg}, provider={provider_pkg})"
                )

if violations:
    print("\n".join(sorted(violations)))
    sys.exit(1)

print("cross-package private import check passed")
PY
