from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imported_symbol_set(module_name: str) -> set[str]:
    symbols: set[str] = set()
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == module_name:
                for alias in node.names:
                    if alias.name != "*":
                        symbols.add(alias.name)
    return symbols


class TestPublicContracts(unittest.TestCase):
    def test_workspace_public_contract_covers_repo_consumers(self) -> None:
        from app.impl.workspace import public

        required = _imported_symbol_set("app.impl.workspace.public")
        missing = sorted(name for name in required if not hasattr(public, name))
        self.assertEqual(missing, [])

    def test_workspace_api_contract_contains_public_contract(self) -> None:
        from app.impl.workspace import api
        workspace_api = api

        required_public_symbols = _imported_symbol_set("app.impl.workspace.public")
        missing_public_in_api = sorted(
            name for name in required_public_symbols if not hasattr(workspace_api, name)
        )
        self.assertEqual(missing_public_in_api, [])

    def test_main_utils_contract_covers_repo_consumers(self) -> None:
        import app.main_util
        main_utils = app.main_util

        required = _imported_symbol_set("app.main_util")
        missing = sorted(name for name in required if not hasattr(main_utils, name))
        self.assertEqual(missing, [])

    def test_dynamic_export_patterns_removed_from_target_modules(self) -> None:
        targets = [
            ROOT / "app" / "impl" / "workspace" / "public.py",
            ROOT / "app" / "impl" / "workspace" / "api.py",
            ROOT / "app" / "main_util.py",
            ROOT / "app" / "impl" / "auth" / "api.py",
        ]
        for path in targets:
            source = path.read_text(encoding="utf-8-sig")
            self.assertNotIn("_export_public(", source, msg=str(path))
            self.assertNotIn("_export_module(", source, msg=str(path))
            self.assertNotIn("for name in dir(", source, msg=str(path))
            self.assertNotIn("[name for name in globals()", source, msg=str(path))


if __name__ == "__main__":
    unittest.main()
