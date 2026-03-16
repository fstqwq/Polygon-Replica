from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = ROOT / "app" / "static" / "style.css"


def _python_files_under(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def _test_case_python_files() -> list[Path]:
    return sorted(path for path in (ROOT / "tests").glob("test_*.py"))


def _app_python_files() -> list[Path]:
    return [
        path
        for path in (ROOT / "app").rglob("*.py")
        if "static/vendor" not in path.as_posix()
    ]


def _test_case_files() -> list[Path]:
    return sorted((ROOT / "tests").glob("test_*.py"))


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


def _is_name_attr(node: ast.AST, *, name: str, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == name and node.attr == attr


def _is_db_handle(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == "db"
    ) or _is_name_attr(node, name="config", attr="db")


class TestPublicContracts(unittest.TestCase):
    def test_danger_link_templates_use_canonical_class(self) -> None:
        expected_patterns = {
            ROOT / "app" / "template" / "run.html": [
                r'class="linkish danger-link" data-submit-form="1">Cancel</a>',
            ],
            ROOT / "app" / "template" / "run_details.html": [
                r'class="linkish danger-link" data-submit-form="1">Cancel</a>',
            ],
            ROOT / "app" / "template" / "history.html": [
                r'class="linkish danger-link"\s+data-submit-form="1"',
            ],
            ROOT / "app" / "template" / "preview.html": [
                r'class="linkish danger-link" data-submit-form="1">Delete</a>',
            ],
            ROOT / "app" / "template" / "solutions.html": [
                r'class="linkish danger-link" data-submit-form="1">Delete</a>',
            ],
            ROOT / "app" / "template" / "tests.html": [
                r'class="linkish danger-link" data-submit-form="1">Delete</a>',
            ],
        }
        for path, patterns in expected_patterns.items():
            source = path.read_text(encoding="utf-8-sig")
            for pattern in patterns:
                self.assertRegex(source, pattern, msg=str(path))

    def test_danger_link_styles_use_shared_tokens(self) -> None:
        source = STYLE_PATH.read_text(encoding="utf-8-sig")
        for token in [
            "--danger-link-color:",
            "--danger-link-hover-color:",
            "--danger-link-hover-bg:",
            "--danger-link-focus:",
            "--danger-link-weight:",
        ]:
            self.assertIn(token, source)
        self.assertIn("color: var(--danger-link-color);", source)
        self.assertIn("font-weight: var(--danger-link-weight);", source)
        self.assertIn("color: var(--danger-link-hover-color);", source)
        self.assertIn("background: var(--danger-link-hover-bg);", source)
        self.assertIn("outline: 2px solid var(--danger-link-focus);", source)

    def test_legacy_danger_link_variants_removed_from_templates_and_styles(self) -> None:
        offenders: list[str] = []
        banned_snippets = [
            'class="linkish danger"',
            "solutions-submit-link",
            "tests-editor-action-link",
            "tests-editor-action-danger",
        ]
        for path in list((ROOT / "app" / "template").rglob("*.html")) + [STYLE_PATH]:
            source = path.read_text(encoding="utf-8-sig")
            for snippet in banned_snippets:
                if snippet in source:
                    offenders.append(f"{path}:{snippet}")
        self.assertEqual(offenders, [])

    def test_workspace_public_wrapper_removed(self) -> None:
        self.assertFalse((ROOT / "app" / "impl" / "workspace" / "public.py").exists())

    def test_auth_public_wrapper_removed(self) -> None:
        self.assertFalse((ROOT / "app" / "impl" / "auth" / "public.py").exists())

    def test_main_utils_contract_covers_repo_consumers(self) -> None:
        import app.main_util

        main_utils = app.main_util
        required = _imported_symbol_set("app.main_util")
        missing = sorted(name for name in required if not hasattr(main_utils, name))
        self.assertEqual(missing, [])

    def test_dynamic_export_patterns_removed_from_target_modules(self) -> None:
        targets = [
            ROOT / "app" / "main_util.py",
            ROOT / "app" / "impl" / "auth" / "api.py",
            ROOT / "app" / "impl" / "workspace" / "context_ui.py",
        ]
        for path in targets:
            source = path.read_text(encoding="utf-8-sig")
            self.assertNotIn("_export_public(", source, msg=str(path))
            self.assertNotIn("_export_module(", source, msg=str(path))
            self.assertNotIn("for name in dir(", source, msg=str(path))
            self.assertNotIn("[name for name in globals()", source, msg=str(path))

    def test_semantic_equivalent_isinstance_rewrites_removed_from_app_modules(self) -> None:
        offenders: list[str] = []
        for path in _app_python_files():
            source = path.read_text(encoding="utf-8-sig")
            if ".__class__ is " in source:
                offenders.append(str(path))
            if "type(" not in source or ") is " not in source:
                continue
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                if len(node.ops) != 1 or len(node.comparators) != 1:
                    continue
                if not isinstance(node.ops[0], ast.Is):
                    continue
                if not isinstance(node.left, ast.Call):
                    continue
                if not isinstance(node.left.func, ast.Name) or node.left.func.id != "type":
                    continue
                offenders.append(f"{path}:{node.lineno}")
                break
        self.assertEqual(offenders, [])

    def test_local_alias_pass_through_assignments_removed_from_app_modules(self) -> None:
        offenders: list[str] = []
        for path in _app_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if len(node.targets) != 1:
                    continue
                target = node.targets[0]
                if not isinstance(target, ast.Name) or not target.id.startswith("_"):
                    continue
                if not isinstance(node.value, ast.Name):
                    continue
                if target.id[1:] != node.value.id:
                    continue
                offenders.append(f"{path}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_pure_forwarding_wrappers_removed_from_app_modules(self) -> None:
        offenders: list[str] = []
        for path in _app_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_"):
                    continue
                if len(node.body) != 1:
                    continue
                stmt = node.body[0]
                if not isinstance(stmt, ast.Return):
                    continue
                if not isinstance(stmt.value, ast.Call):
                    continue
                if not isinstance(stmt.value.func, ast.Name):
                    continue
                params = [arg.arg for arg in node.args.args]
                args: list[str] = []
                for arg in stmt.value.args:
                    if not isinstance(arg, ast.Name):
                        args = []
                        break
                    args.append(arg.id)
                if node.name[1:] != stmt.value.func.id:
                    continue
                if stmt.value.keywords or params != args:
                    continue
                offenders.append(f"{path}:{node.lineno}")
        self.assertEqual(offenders, [])

    def test_impl_modules_do_not_issue_direct_sql(self) -> None:
        offenders: list[str] = []
        direct_sql_tokens = ("config" + ".db.", "." + "fetch_one(", "." + "fetch_all(", "." + "execute(", "write_" + "transaction(")
        for path in _python_files_under(ROOT / "app" / "impl"):
            source = path.read_text(encoding="utf-8-sig")
            for token in direct_sql_tokens:
                if token in source:
                    offenders.append(f"{path}:{token}")
        self.assertEqual(offenders, [])

    def test_test_case_modules_do_not_issue_direct_sql(self) -> None:
        offenders: list[str] = []
        for path in _test_case_files():
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if _is_db_handle(node.func.value) and node.func.attr in {"fetch_one", "fetch_all", "execute", "write_transaction"}:
                        offenders.append(f"{path}:{node.lineno}:{node.func.attr}")
        self.assertEqual(offenders, [])

    def test_test_modules_do_not_issue_direct_sql(self) -> None:
        offenders: list[str] = []
        direct_sql_tokens = (
            "config" + ".db.",
            "db" + ".fetch_one(",
            "db" + ".fetch_all(",
            "db" + ".execute(",
            "db" + ".write_transaction(",
        )
        for path in _test_case_python_files():
            source = path.read_text(encoding="utf-8-sig")
            for token in direct_sql_tokens:
                if token in source:
                    offenders.append(f"{path}:{token}")
        self.assertEqual(offenders, [])

    def test_only_allowed_modules_import_private_persistence(self) -> None:
        allowed = {
            ROOT / "app" / "impl" / "runtime" / "config.py",
            ROOT / "app" / "service" / "auth" / "service.py",
            ROOT / "app" / "service" / "contest" / "service.py",
            ROOT / "app" / "service" / "export" / "api.py",
            ROOT / "app" / "service" / "judgehost" / "api.py",
            ROOT / "app" / "service" / "judgehost" / "internal" / "domjudge_dispatch.py",
            ROOT / "app" / "service" / "repository" / "workspace.py",
            ROOT / "app" / "service" / "runtime" / "state_service.py",
            ROOT / "app" / "service" / "statement" / "preview.py",
            ROOT / "app" / "service" / "verification" / "pipeline.py",
            ROOT / "app" / "service" / "verification" / "service.py",
            ROOT / "app" / "service" / "verification" / "store.py",
            ROOT / "app" / "service" / "platform" / "system_config.py",
        }
        offenders: list[str] = []
        for path in _python_files_under(ROOT / "app"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module_name = str(node.module or "")
                    if module_name.startswith("app.service.disk.") or module_name.startswith("app.service.memory."):
                        if path not in allowed:
                            offenders.append(f"{path}:{node.lineno}:{module_name}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_name = alias.name
                        if module_name.startswith("app.service.disk.") or module_name.startswith("app.service.memory."):
                            if path not in allowed:
                                offenders.append(f"{path}:{node.lineno}:{module_name}")
        self.assertEqual(offenders, [])

    def test_sqlite_row_does_not_leak_outside_private_persistence(self) -> None:
        allowed = {
            ROOT / "app" / "db.py",
        }
        offenders: list[str] = []
        for path in _python_files_under(ROOT / "app"):
            if "/service/memory/" in path.as_posix():
                continue
            source = path.read_text(encoding="utf-8-sig")
            if "sqlite3.Row" in source and path not in allowed:
                offenders.append(str(path))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
