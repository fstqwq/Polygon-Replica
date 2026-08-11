from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.service.importing.native import NativePackageImportService
from app.service.problem_package.manifest import (
    NativeManifest,
    describe_file,
)
from app.service.problem_package.service import NativePackageReader
from app.service.problem_package.statement_samples import (
    hydrate_native_statement_samples,
)
from app.service.problem_package.store import MaterializationRow
from tests.archive_support import import_problem_package


class TestExportService(unittest.TestCase):
    def test_statement_sample_hydration_limits_each_input_output_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="statement-sample-budget-") as temp:
            package_root = Path(temp) / "native"
            spec_path = package_root / "tests" / "spec.json"
            spec_path.parent.mkdir(parents=True)
            spec_payload = {
                "tests": [{"id": "001", "kind": "manual", "sample": True}]
            }
            sample_limit = 1024
            document_limit = 2048
            original_spec = json.dumps(spec_payload, indent=2) + "\n"
            self.assertLess(len(original_spec.encode("utf-8")), document_limit)
            spec_path.write_text(original_spec, encoding="utf-8")

            test_data = package_root / "test_data" / "tests" / "001"
            test_data.mkdir(parents=True)
            input_path = test_data / "input"
            answer_path = test_data / "answer"
            large_input = b"x" * 600
            large_answer = b"y" * 600
            self.assertLessEqual(len(large_input), sample_limit)
            self.assertLessEqual(len(large_answer), sample_limit)
            self.assertGreater(len(large_input) + len(large_answer), sample_limit)
            input_path.write_bytes(large_input)
            answer_path.write_bytes(large_answer)

            materialization: MaterializationRow = {
                "id": "pm-test",
                "problem_id": 1,
                "source_commit": "a" * 40,
                "revision_number": 1,
                "source_digest": "b" * 64,
                "archive_rel_path": "native.zip",
                "archive_sha256": "c" * 64,
                "archive_size_bytes": 1,
                "verification_id": "ver-1",
                "status": "available",
                "created_at": "2026-01-01T00:00:00Z",
                "checked_at": "2026-01-01T00:00:00Z",
                "unavailable_reason": "",
            }
            manifest: NativeManifest = {
                "source_commit": materialization["source_commit"],
                "revision_number": materialization["revision_number"],
                "source_digest": materialization["source_digest"],
                "mode": "pass-fail",
                "pass_limit": 1,
                "verification": {"id": materialization["verification_id"]},
                "tests": [
                    {
                        "id": "001",
                        "kind": "manual",
                        "sample": True,
                        "input": describe_file(input_path, root=package_root),
                        "answer": describe_file(answer_path, root=package_root),
                    }
                ],
            }
            native = NativePackageReader(
                materialization=materialization,
                root=package_root,
                manifest=manifest,
            )

            with self.assertRaisesRegex(
                ValueError,
                "statement sample exceeds byte limit",
            ):
                hydrate_native_statement_samples(
                    native,
                    tests_spec_max_bytes=document_limit,
                    statement_sample_max_bytes=sample_limit,
                )
            self.assertEqual(spec_path.read_text(encoding="utf-8"), original_spec)

            input_path.write_bytes(b"123456")
            answer_path.write_bytes(b"abcdef")
            manifest_test = manifest["tests"][0]
            manifest_test["input"] = describe_file(input_path, root=package_root)
            manifest_test["answer"] = describe_file(answer_path, root=package_root)
            hydrate_native_statement_samples(
                native,
                tests_spec_max_bytes=document_limit,
                statement_sample_max_bytes=sample_limit,
            )
            hydrated = json.loads(spec_path.read_text(encoding="utf-8"))["tests"][0]
            self.assertEqual(
                (hydrated["sample_input"], hydrated["sample_output"]),
                ("123456", "abcdef"),
            )

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

            import_problem_package(
                NativePackageImportService(),
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

            import_problem_package(
                NativePackageImportService(),
                workspace,
                archive.name,
                archive.read_bytes(),
            )

            self.assertTrue((workspace / "solutions" / "restored.cpp").is_file())
            self.assertFalse((workspace / "test_data").exists())
