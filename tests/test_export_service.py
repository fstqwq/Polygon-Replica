import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.service.disk.export_store import ExportJobRow
from app.service.export.service import ExportService
from app.service.importing.polygon_replica import PolygonReplicaPackageImportService
from app.service.problem_package.manifest import (
    VerifiedRevisionManifest,
    describe_file,
)
from app.service.problem_package.service import VerifiedRevisionReader
from app.service.problem_package.statement_samples import (
    hydrate_verified_statement_samples,
)
from app.service.problem_package.store import MaterializationRow
from tests.archive_support import import_problem_package


class TestExportService(unittest.TestCase):
    def test_failed_unstarted_export_job_still_reports_queued_phase(self) -> None:
        job: ExportJobRow = {
            "id": "exp-not-submitted",
            "problem_id": 1,
            "actor_user_id": 1,
            "export_type": "domjudge",
            "source_commit": "a" * 40,
            "status": "failed",
            "materialization_id": "",
            "export_id": "",
            "error": "queue rejected",
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": "",
            "finished_at": "2026-01-01T00:00:01Z",
            "filename": "",
            "sha256": "",
            "size_bytes": 0,
        }

        self.assertEqual(ExportService.job_phase(job), "queued")

    def test_statement_sample_hydration_limits_each_input_output_pair(self) -> None:
        with tempfile.TemporaryDirectory(prefix="statement-sample-budget-") as temp:
            package_root = Path(temp) / "verified-revision"
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
            manifest: VerifiedRevisionManifest = {
                "source_commit": materialization["source_commit"],
                "revision_number": materialization["revision_number"],
                "source_digest": materialization["source_digest"],
                "mode": "pass-fail",
                "pass_limit": 1,
                "verification": {
                    "id": materialization["verification_id"],
                    "source": "full-verification",
                },
                "solutions": [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                        "verdicts": ["AC"],
                    }
                ],
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
            revision = VerifiedRevisionReader(
                verified_revision=materialization,
                root=package_root,
                manifest=manifest,
            )

            with self.assertRaisesRegex(
                ValueError,
                "statement sample exceeds byte limit",
            ):
                hydrate_verified_statement_samples(
                    revision,
                    tests_spec_max_bytes=document_limit,
                    statement_sample_max_bytes=sample_limit,
                )
            self.assertEqual(spec_path.read_text(encoding="utf-8"), original_spec)

            input_path.write_bytes(b"123456")
            answer_path.write_bytes(b"abcdef")
            manifest_test = manifest["tests"][0]
            manifest_test["input"] = describe_file(input_path, root=package_root)
            manifest_test["answer"] = describe_file(answer_path, root=package_root)
            hydrate_verified_statement_samples(
                revision,
                tests_spec_max_bytes=document_limit,
                statement_sample_max_bytes=sample_limit,
            )
            hydrated = json.loads(spec_path.read_text(encoding="utf-8"))["tests"][0]
            self.assertEqual(
                (hydrated["sample_input"], hydrated["sample_output"]),
                ("123456", "abcdef"),
            )

    def test_polygon_replica_import_rejects_source_only_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-source-only-") as temp:
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

            with self.assertRaisesRegex(
                ValueError,
                "test_data/manifest.json",
            ):
                import_problem_package(
                    PolygonReplicaPackageImportService(),
                    workspace,
                    archive.name,
                    archive.read_bytes(),
                )

            self.assertEqual(
                (workspace / "old.txt").read_text(encoding="utf-8"),
                "old\n",
            )

    def test_polygon_replica_import_rejects_partial_verified_data(self) -> None:
        with tempfile.TemporaryDirectory(prefix="polygon-replica-partial-") as temp:
            archive = Path(temp) / "partial.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("config/problem.json", "{}\n")
                package.writestr("solutions/restored.cpp", "// restored\n")
                package.writestr("test_data/tests/001/input", "1\n")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            (workspace / "old.txt").write_text("old\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "test_data/manifest.json",
            ):
                import_problem_package(
                    PolygonReplicaPackageImportService(),
                    workspace,
                    archive.name,
                    archive.read_bytes(),
                )

            self.assertEqual(
                (workspace / "old.txt").read_text(encoding="utf-8"),
                "old\n",
            )
