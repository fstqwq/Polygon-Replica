import ast
import hashlib
import logging
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_NON_ASCII_TEST_PRAGMA = b"ascii-lint: allow; reason=chinese-test"

_FIRST_PARTY_TEXT_SUFFIXES = frozenset(
    {
        ".cfg",
        ".conf",
        ".cpp",
        ".css",
        ".h",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".md",
        ".py",
        ".service",
        ".sh",
        ".sql",
        ".sty",
        ".svg",
        ".tex",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_ROOT_TEXT_FILENAMES = frozenset(
    {
        ".gitattributes",
        ".gitignore",
        "Dockerfile",
        "LICENSE",
    }
)


def _python_files_under(root: Path) -> list[Path]:
    return [path for path in root.rglob("*.py") if "__pycache__" not in path.parts]


def _test_case_python_files() -> list[Path]:
    return sorted(path for path in (ROOT / "tests").glob("test_*.py"))


def _test_case_files() -> list[Path]:
    return sorted((ROOT / "tests").glob("test_*.py"))


def _first_party_text_files() -> list[Path]:
    paths: list[Path] = []
    for root_name in ("app", "docs", "scripts", "tests", ".github"):
        source_root = ROOT / root_name
        if not source_root.exists():
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _FIRST_PARTY_TEXT_SUFFIXES:
                continue
            relative = path.relative_to(ROOT)
            if relative.as_posix().startswith("app/static/vendor/"):
                continue
            paths.append(path)
    for path in ROOT.iterdir():
        if not path.is_file():
            continue
        if path.name in _ROOT_TEXT_FILENAMES or path.suffix.lower() in _FIRST_PARTY_TEXT_SUFFIXES:
            paths.append(path)
    return sorted(set(paths))


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
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
        and node.attr == attr
    )


def _is_db_handle(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "db") or _is_name_attr(
        node, name="config", attr="db"
    )


class TestPublicContracts(unittest.TestCase):
    def test_first_party_text_files_are_ascii(self) -> None:
        offenders: list[str] = []
        invalid_pragmas: list[str] = []
        for path in _first_party_text_files():
            payload = path.read_bytes()
            relative = path.relative_to(ROOT)
            has_non_ascii = any(byte > 0x7F for byte in payload)
            header = b"\n".join(payload.splitlines()[:5])
            has_pragma = _NON_ASCII_TEST_PRAGMA in header
            if has_pragma and (
                not relative.parts or relative.parts[0] != "tests" or not has_non_ascii
            ):
                invalid_pragmas.append(relative.as_posix())
            if has_non_ascii and not has_pragma:
                offenders.append(relative.as_posix())
        self.assertEqual(
            offenders,
            [],
            "Use ASCII escapes/entities, or add the file-header pragma "
            "'# ascii-lint: allow; reason=chinese-test' to an intentional test.",
        )
        self.assertEqual(
            invalid_pragmas,
            [],
            "The non-ASCII pragma is test-only and must not remain on an ASCII file.",
        )

    def test_static_assets_use_startup_content_fingerprints(self) -> None:
        from app.service.platform.static_assets import StaticAssetManifest

        with tempfile.TemporaryDirectory() as temporary_directory:
            static_root = Path(temporary_directory)
            asset_path = static_root / "nested" / "space +&?#%.js"
            asset_path.parent.mkdir()
            asset_path.write_bytes(b"first version")
            expected_digest = hashlib.sha256(b"first version").hexdigest()[:12]

            manifest = StaticAssetManifest(static_root)
            self.assertEqual(
                manifest.url("nested/space +&?#%.js"),
                f"/static/nested/space%20%2B%26%3F%23%25.js?v={expected_digest}",
            )

            asset_path.write_bytes(b"second version")
            refreshed = StaticAssetManifest(static_root)
            self.assertNotEqual(
                manifest.url("nested/space +&?#%.js"),
                refreshed.url("nested/space +&?#%.js"),
            )

            for invalid_path in [
                "",
                "/nested/file.js",
                "nested//file.js",
                "nested/./file.js",
                "../file.js",
                "nested\\file.js",
                "missing.js",
            ]:
                with self.subTest(invalid_path=invalid_path):
                    with self.assertRaises(ValueError):
                        manifest.url(invalid_path)

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

    def test_main_utils_contract_covers_repo_consumers(self) -> None:
        import app.main_util

        main_utils = app.main_util
        required = _imported_symbol_set("app.main_util")
        missing = sorted(name for name in required if not hasattr(main_utils, name))
        self.assertEqual(missing, [])

    def test_impl_modules_do_not_issue_direct_sql(self) -> None:
        offenders: list[str] = []
        direct_sql_tokens = (
            "config" + ".db.",
            "." + "fetch_one(",
            "." + "fetch_all(",
            "." + "execute(",
            "write_" + "transaction(",
        )
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
                    if _is_db_handle(node.func.value) and node.func.attr in {
                        "fetch_one",
                        "fetch_all",
                        "execute",
                        "write_transaction",
                    }:
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
            ROOT / "app" / "runtime.py",
            ROOT / "app" / "service" / "auth" / "service.py",
            ROOT / "app" / "service" / "contest" / "service.py",
            ROOT / "app" / "service" / "export" / "service.py",
            ROOT / "app" / "service" / "judgehost" / "work" / "dispatch.py",
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
                    if module_name.startswith("app.service.disk.") or module_name.startswith(
                        "app.service.memory."
                    ):
                        if path not in allowed:
                            offenders.append(f"{path}:{node.lineno}:{module_name}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_name = alias.name
                        if module_name.startswith("app.service.disk.") or module_name.startswith(
                            "app.service.memory."
                        ):
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
