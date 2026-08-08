from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.impl.runtime.config import config
from app.service.importing.native import NativePackageImportService
from app.service.problem_package.manifest import load_manifest, validate_manifest_files
from tests.common import E2ETestBase
from tests.db_helpers import db_execute, db_fetch_one


class TestPublishedRevisionExport(E2ETestBase):
    def _publish_problem(self, *, test_id: str = "001") -> tuple[Path, int, str]:
        workspace = Path(self._workspace_path())
        (workspace / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (workspace / "tests" / "manual" / f"{test_id}.in").write_text("1\n", encoding="utf-8")
        (workspace / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "tests": [
                        {
                            "id": test_id,
                            "kind": "manual",
                            "sample": True,
                            "sample_input": "display input\n",
                            "sample_output": "display output\n",
                        }
                    ]
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        accepted = workspace / "solutions" / "accepted.cpp"
        accepted.write_text("int main() { return 0; }\n", encoding="utf-8")
        (workspace / "solutions" / "accepted.cpp.desc").write_text(
            "expected: accepted\n",
            encoding="utf-8",
        )
        commit = config.git_service.commit(
            workspace,
            "publish materialization fixture",
            self.user,
            f"{self.user}@polygonlike.local",
        )
        config.git_service.push(workspace, "main")
        context = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
        return workspace, int(context["problem"]["id"]), commit

    @staticmethod
    def _verification_builder(problem_id: int, *, input_bytes: bytes = b"1\n", answer_bytes: bytes = b"2\n"):
        def build(_snapshot: Path, commit: str, _revision_number: int, verification_id: str) -> str:
            config.verification_service.begin_verification_record(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=None,
                signature="materialization-test",
                source_commit=commit,
                kind="all",
                status="ok",
            )
            input_ref = config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role="input",
                file_name="001.in",
                payload=input_bytes,
            )
            answer_ref = config.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role="answer",
                file_name="001.ans",
                payload=answer_bytes,
            )
            config.verification_service.update_verification_artifact_refs(
                verification_id,
                "001.in",
                {"input_ref": input_ref, "answer_ref": answer_ref},
            )
            return verification_id

        return build

    def _materialize(self):
        _workspace, problem_id, commit = self._publish_problem()
        revision = config.problem_package_service.published_revision(problem_id)
        self.assertEqual(revision.source_commit, commit)
        materialization = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        return problem_id, commit, materialization

    def test_native_is_the_published_git_revision_plus_materialized_test_data(self) -> None:
        workspace, problem_id, commit = self._publish_problem()
        revision = config.problem_package_service.published_revision(problem_id)
        materialization = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        (workspace / "dirty-only.txt").write_text("must not be exported\n", encoding="utf-8")

        stored, archive = config.problem_package_service.native_archive(materialization["id"])
        self.assertEqual(stored["source_commit"], commit)
        with zipfile.ZipFile(archive, "r") as package:
            names = set(package.namelist())
            self.assertIn("config/problem.json", names)
            self.assertIn("test_data/manifest.json", names)
            self.assertIn("test_data/tests/001/input", names)
            self.assertIn("test_data/tests/001/answer", names)
            self.assertIn("test_data/tests/001/sample-input", names)
            self.assertIn("test_data/tests/001/sample-output", names)
            self.assertNotIn("source/config/problem.json", names)
            self.assertNotIn("dirty-only.txt", names)
            manifest = json.loads(package.read("test_data/manifest.json"))
            self.assertEqual(manifest["source_commit"], commit)
            self.assertEqual(package.read("test_data/tests/001/input"), b"1\n")
            self.assertEqual(package.read("test_data/tests/001/answer"), b"2\n")

    def test_same_revision_reuses_native_and_missing_archive_is_rematerialized(self) -> None:
        problem_id, _commit, first = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        second = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        self.assertEqual(second["id"], first["id"])

        _stored, archive = config.problem_package_service.native_archive(first["id"])
        archive.unlink()
        self.assertIsNone(
            config.problem_package_service.available_materialization(problem_id, revision.source_commit)
        )
        restored = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        self.assertEqual(restored["id"], first["id"])
        self.assertTrue(config.problem_package_service.native_archive(restored["id"])[1].is_file())

    def test_icpc_conversion_only_needs_native_after_verification_is_deleted(self) -> None:
        problem_id, commit, materialization = self._materialize()
        db_execute(
            "DELETE FROM verification_artifact_refs WHERE verification_id=?",
            [materialization["verification_id"]],
        )
        db_execute("DELETE FROM verifications WHERE id=?", [materialization["verification_id"]])

        def compile_statement(_snapshot: Path, destination: Path, **_kwargs: object) -> bool:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "problem.en.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            return True

        with patch.object(config.export_service, "_try_compile_statement_pdf", side_effect=compile_statement):
            export_id, archive = config.export_service.create_export(
                self.problem,
                "icpc",
                materialization_id=materialization["id"],
            )
        self.assertTrue(export_id)
        with zipfile.ZipFile(archive, "r") as package:
            metadata = package.read("problem.yaml").decode("utf-8")
            self.assertIn(commit, metadata)
            self.assertIn("data/sample/001.in", package.namelist())
            self.assertIn("data/sample/001.ans", package.namelist())

        repeated_id, repeated_archive = config.export_service.create_export(
            self.problem,
            "icpc",
            materialization_id=materialization["id"],
        )
        self.assertEqual(repeated_id, export_id)
        self.assertEqual(repeated_archive, archive)
        count = db_fetch_one(
            "SELECT COUNT(*) AS c FROM exports WHERE materialization_id=? AND export_type='icpc'",
            [materialization["id"]],
        )
        self.assertEqual(int(count["c"]), 1)
        self.assertEqual(problem_id, materialization["problem_id"])

    def test_native_import_validates_then_discards_materialized_data(self) -> None:
        _problem_id, _commit, materialization = self._materialize()
        _stored, archive = config.problem_package_service.native_archive(materialization["id"])
        with tempfile.TemporaryDirectory(prefix="native-import-") as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            NativePackageImportService().import_package(
                workspace,
                archive.name,
                archive.read_bytes(),
            )
            self.assertTrue((workspace / "config" / "problem.json").is_file())
            self.assertFalse((workspace / "test_data").exists())
            self.assertFalse((workspace / "tests" / "answers").exists())

    def test_native_reader_rejects_manifest_tampering(self) -> None:
        _problem_id, _commit, materialization = self._materialize()
        with config.problem_package_service.open_reader(materialization["id"]) as native:
            manifest_path = native.root / "test_data" / "manifest.json"
            manifest = load_manifest(manifest_path)
            payload = native.root / "test_data" / "tests" / "001" / "input"
            payload.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ValueError, "integrity"):
                validate_manifest_files(native.root, manifest)
        stored = config.problem_package_service.store.materialization(materialization["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "available")


if __name__ == "__main__":
    raise SystemExit("run through the Linux test suite")
