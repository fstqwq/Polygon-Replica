from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.service.importing.native import NativePackageImportService


class TestExportService(unittest.TestCase):
    def test_native_import_accepts_source_only_archive_with_package_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-source-only-") as temp:
            archive = Path(temp) / "source-only.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(
                    "sample-snapshot/config/problem.json",
                    '{"memory_limit_mb": 1}\n',
                )
                package.writestr(
                    "sample-snapshot/solutions/backup.cpp",
                    "// restored\n",
                )
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            (workspace / "old.txt").write_text("old\n", encoding="utf-8")

            NativePackageImportService().import_package(
                workspace,
                archive.name,
                archive.read_bytes(),
            )

            problem_config = json.loads(
                (workspace / "config" / "problem.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(problem_config["memory_limit_mb"], 1)
            self.assertEqual(
                (workspace / "solutions" / "backup.cpp").read_text(
                    encoding="utf-8"
                ),
                "// restored\n",
            )
            self.assertFalse((workspace / "old.txt").exists())

    def test_native_import_ignores_partial_materialized_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-partial-data-") as temp:
            archive = Path(temp) / "partial.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("config/problem.json", "{}\n")
                package.writestr("solutions/restored.cpp", "// restored\n")
                package.writestr("test_data/tests/001/input", "1\n")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()

            NativePackageImportService().import_package(
                workspace,
                archive.name,
                archive.read_bytes(),
            )

            self.assertTrue((workspace / "solutions" / "restored.cpp").is_file())
            self.assertFalse((workspace / "test_data").exists())
