from __future__ import annotations

import ast
import logging
import re
import unittest
from pathlib import Path
from tests.assertion_helpers import assert_html_contract

ROOT = Path(__file__).resolve().parents[1]
STYLE_PATH = ROOT / "app" / "static" / "style.css"
TOKENS_PATH = ROOT / "app" / "static" / "css" / "00_tokens.css"
WORKSPACE_CSS_PATH = ROOT / "app" / "static" / "css" / "20_workspace.css"
UI_JS_PATH = ROOT / "app" / "static" / "ui.js"
SETTINGS_TEMPLATE_PATH = ROOT / "app" / "template" / "settings.html"


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
    def test_ui_colors_are_owned_by_global_tokens(self) -> None:
        color_literal = re.compile(r"#[0-9a-fA-F]{3,8}\b|(?:rgb|hsl)a?\(")
        offenders: list[str] = []
        for path in sorted((ROOT / "app" / "static" / "css").glob("*.css")):
            if path.name == "00_tokens.css":
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
                if color_literal.search(line):
                    offenders.append(f"{path.name}:{line_number}")
        self.assertEqual(offenders, [])

        token_source = TOKENS_PATH.read_text(encoding="utf-8-sig")
        for stale_color in ["#f6eff4", "#fffafd", "#d8c4d3", "#6d3a5f"]:
            self.assertNotIn(stale_color, token_source.lower())

    def test_code_editor_assets_have_one_lazy_loading_owner(self) -> None:
        base_source = (ROOT / "app" / "template" / "base.html").read_text(encoding="utf-8-sig")
        editor_source = (ROOT / "app" / "static" / "editor_init.js").read_text(encoding="utf-8-sig")
        ui_source = UI_JS_PATH.read_text(encoding="utf-8-sig")
        forms_source = (ROOT / "app" / "static" / "css" / "30_forms.css").read_text(encoding="utf-8-sig")
        tests_source = (ROOT / "app" / "static" / "css" / "50_tests.css").read_text(encoding="utf-8-sig")

        self.assertNotIn("/static/vendor/codemirror", base_source)
        self.assertIn('var TARGET_SELECTOR = "textarea[data-code-editor=\'1\']";', editor_source)
        self.assertIn('var EDITOR_READY_EVENT = "polygonlike:code-editor-ready";', editor_source)
        self.assertEqual(ui_source.count("function findCodeMirrorEditorForTextarea("), 1)
        self.assertNotIn("[400, 1200, 2400]", ui_source)
        self.assertNotIn("[400, 1200, 2400, 4800]", ui_source)
        self.assertIn(".component-editor-error {", forms_source)
        self.assertNotIn(".component-editor-error {", tests_source)

    def test_run_detail_popup_title_uses_safe_canonical_test_metadata(self) -> None:
        ui_source = UI_JS_PATH.read_text(encoding="utf-8-sig")

        self.assertIn('document.createTextNode("Test Details: " + testName)', ui_source)
        self.assertIn('commandText.className = "verification-test-title-command";', ui_source)
        self.assertIn('commandText.textContent = command || "gen";', ui_source)
        self.assertNotIn("solutionTitle", ui_source)
        self.assertNotIn("popupTitle.innerHTML", ui_source)

    def test_generated_judgehost_commands_isolate_submission_uids(self) -> None:
        ui_source = UI_JS_PATH.read_text(encoding="utf-8-sig")
        settings_source = SETTINGS_TEMPLATE_PATH.read_text(encoding="utf-8-sig")

        self.assertIn("return safe >= 1 && safe <= 65533 ? safe : 60706;", ui_source)
        self.assertIn("var runUserUidGid = runUidBase + daemonId;", ui_source)
        self.assertIn("if (largestRunUidGid > 65533)", ui_source)
        self.assertIn("largestRunUidGid > 61183", ui_source)
        self.assertIn('" -e RUN_USER_UID_GID=" +', ui_source)
        self.assertIn('data-gen-script-run-uid-base="1"', settings_source)
        self.assertIn("base + DAEMON_ID", settings_source)

    def test_generated_judgehost_command_uses_unconfigured_latest_image(self) -> None:
        command_source = UI_JS_PATH.read_text(encoding="utf-8-sig")
        documentation = "\n".join(
            [
                (ROOT / "README.md").read_text(encoding="utf-8-sig"),
                (ROOT / "docs" / "docker.md").read_text(encoding="utf-8-sig"),
            ]
        )

        self.assertIn("domjudge/judgehost:latest", command_source)
        self.assertIn("domjudge/judgehost:latest", documentation)
        self.assertNotIn("domjudge/judgehost:9.0.0", command_source)
        self.assertNotIn("domjudge/judgehost:9.0.0", documentation)
        self.assertNotIn("POLYGON_REPLICA_JUDGEHOST_IMAGE", command_source)

    def test_uvicorn_access_filter_only_suppresses_successful_fetch_poll(self) -> None:
        from app.service.platform.http_logging import UvicornAccessFilter

        access_filter = UvicornAccessFilter()

        def access_record(method: str, path: str, status_code: int) -> logging.LogRecord:
            return logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                __file__,
                1,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:1", method, path, "1.1", status_code),
                None,
            )

        fetch_path = "/api/v4/judgehosts/fetch-work"
        self.assertFalse(access_filter.filter(access_record("POST", fetch_path, 200)))
        self.assertTrue(access_filter.filter(access_record("POST", fetch_path, 400)))
        self.assertTrue(access_filter.filter(access_record("GET", fetch_path, 200)))
        self.assertTrue(access_filter.filter(access_record("POST", "/login", 200)))

    def test_server_entrypoints_outlive_judgedaemon_fetch_interval(self) -> None:
        local_script = (ROOT / "scripts" / "start_local.sh").read_text(encoding="utf-8")
        docker_script = (ROOT / "scripts" / "docker-entrypoint.sh").read_text(encoding="utf-8")
        systemd_unit = (ROOT / "scripts" / "systemd" / "polygon-replica.service").read_text(
            encoding="utf-8"
        )

        self.assertIn("POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC:-30", local_script)
        self.assertIn("POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC:-30", docker_script)
        self.assertIn("--timeout-keep-alive 30", systemd_unit)

    def test_danger_link_templates_use_canonical_class(self) -> None:
        expected_patterns = {
            ROOT / "app" / "template" / "run.html": [
                r'class="linkish danger-link" data-submit-form="1">Cancel</a>',
            ],
            ROOT / "app" / "template" / "run_details.html": [
                r'class="linkish danger-link" data-submit-form="1">Cancel</a>',
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
        token_source = TOKENS_PATH.read_text(encoding="utf-8-sig")
        workspace_source = WORKSPACE_CSS_PATH.read_text(encoding="utf-8-sig")
        for token in [
            "--danger-link-color:",
            "--danger-link-hover-color:",
            "--danger-link-hover-bg:",
            "--danger-link-focus:",
            "--danger-link-weight:",
        ]:
            self.assertIn(token, token_source)
        self.assertIn("color: var(--danger-link-color);", workspace_source)
        self.assertIn("font-weight: var(--danger-link-weight);", workspace_source)
        self.assertIn("color: var(--danger-link-hover-color);", workspace_source)
        self.assertIn("background: var(--danger-link-hover-bg);", workspace_source)
        self.assertIn("outline: 2px solid var(--danger-link-focus);", workspace_source)

    def test_merge_ui_uses_result_language_and_has_no_default_file_choice(self) -> None:
        source = (ROOT / "app" / "template" / "merge.html").read_text(encoding="utf-8")
        assert_html_contract(
            self,
            source,
            contains=(
                "choose files one by one",
                "Update workspace",
                "Use the published file",
                "Keep the workspace file",
                "Fast-forward possible.",
            ),
            excludes=(
                "Choose complete files instead",
                "Keep my files unchanged",
                "Each comparison shows",
                "Review and choose",
                "Confirm update",
                "Confirm File Update",
                "merge-file-browser",
                "merge-review-layout",
                "data-selected-entry",
                'value="published" required checked',
                'value="workspace" required checked',
                "File group",
                "Step 3",
                "latest shared",
                "current files",
            ),
            label="merge template contract",
        )
        for implementation_term in ["rebase", "commit hash", "bare repo", "uncommitted"]:
            self.assertNotIn(implementation_term, source.lower())

        script = (ROOT / "app" / "static" / "merge.js").read_text(encoding="utf-8")
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)

        routes = (ROOT / "app" / "route" / "problem_route.py").read_text(encoding="utf-8")
        for suffix in ["/merge/{preview_id}/review", "/merge/{preview_id}/edit", "/merge/{preview_id}/cancel"]:
            self.assertNotIn(suffix, routes)

    def test_old_danger_link_variants_removed_from_templates_and_styles(self) -> None:
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
            ROOT / "app" / "impl" / "auth" / "middleware.py",
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
            ROOT / "app" / "service" / "export" / "service.py",
            ROOT / "app" / "service" / "judgehost" / "dispatch.py",
            ROOT / "app" / "service" / "judgehost" / "state.py",
            ROOT / "app" / "service" / "mail" / "smtp_config.py",
            ROOT / "app" / "service" / "repository" / "workspace.py",
            ROOT / "app" / "service" / "runtime" / "state_service.py",
            ROOT / "app" / "service" / "statement" / "preview.py",
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
