import hashlib
import logging
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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
_INTENTIONALLY_NON_ASCII_TEXT_PATHS = frozenset(
    {
        Path("docs/user-guide.md"),
    }
)


def _production_text_files() -> list[Path]:
    paths: list[Path] = []
    for root_name in ("app", "docs", "scripts", ".github"):
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


class TestPublicContracts(unittest.TestCase):
    def test_docker_build_context_excludes_environment_files(self) -> None:
        rules = {
            line.strip()
            for line in (ROOT / ".dockerignore")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        required_rules = {".env", ".env.*", "**/.env", "**/.env.*"}

        self.assertEqual(
            required_rules - rules,
            set(),
            "Docker build contexts must exclude root and nested environment files.",
        )

    def test_production_text_files_are_ascii(self) -> None:
        offenders: list[str] = []
        for path in _production_text_files():
            payload = path.read_bytes()
            relative = path.relative_to(ROOT)
            has_non_ascii = any(byte > 0x7F for byte in payload)
            if has_non_ascii and relative not in _INTENTIONALLY_NON_ASCII_TEXT_PATHS:
                offenders.append(relative.as_posix())
        self.assertEqual(
            offenders,
            [],
            "Use ASCII escapes or entities in first-party production text files.",
        )

    def test_static_assets_use_startup_content_fingerprints(self) -> None:
        from app.service.platform.static_assets import StaticAssetManifest

        with tempfile.TemporaryDirectory() as temporary_directory:
            static_root = Path(temporary_directory) / "static"
            static_root.mkdir()
            asset_path = static_root / "nested" / "space +&#%.js"
            asset_path.parent.mkdir()
            asset_path.write_bytes(b"first version")
            outside = Path(temporary_directory) / "private.txt"
            outside.write_bytes(b"outside static root")
            (static_root / "external.js").symlink_to(outside)
            expected_digest = hashlib.sha256(b"first version").hexdigest()[:12]

            manifest = StaticAssetManifest(static_root)
            self.assertEqual(
                manifest.url("nested/space +&#%.js"),
                f"/static/nested/space%20%2B%26%23%25.js?v={expected_digest}",
            )

            asset_path.write_bytes(b"second version")
            refreshed = StaticAssetManifest(static_root)
            self.assertNotEqual(
                manifest.url("nested/space +&#%.js"),
                refreshed.url("nested/space +&#%.js"),
            )

            for invalid_path in [
                "",
                "/nested/file.js",
                "nested//file.js",
                "nested/./file.js",
                "../file.js",
                "nested\\file.js",
                "missing.js",
                "external.js",
            ]:
                with self.subTest(invalid_path=invalid_path):
                    with self.assertRaises(ValueError):
                        manifest.url(invalid_path)

    def test_page_favicons_use_stable_disjoint_major_arcana_ranges(self) -> None:
        from app.service.platform.favicon import (
            contest_favicon_asset,
            problem_favicon_asset,
        )

        self.assertEqual(
            problem_favicon_asset("alice/example"),
            "favicon/major-arcana/04.png",
        )
        self.assertEqual(
            contest_favicon_asset("world-finals"),
            "favicon/major-arcana/20.png",
        )

        static_root = ROOT / "app" / "static"
        arcana_root = static_root / "favicon" / "major-arcana"
        self.assertEqual(
            sorted(path.name for path in arcana_root.glob("*.png")),
            [f"{index:02d}.png" for index in range(22)],
        )
        self.assertEqual(
            (static_root / "favicon.png").read_bytes(),
            (arcana_root / "00.png").read_bytes(),
        )

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

if __name__ == "__main__":
    unittest.main()
