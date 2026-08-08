from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.impl.runtime.config import config
from app.service.export.icpc_package import SUBMISSION_RULES
from app.service.importing.native import NativePackageImportService
from app.service.platform.git_process import run_git
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

    def test_latest_succeeded_export_job_matches_commit_and_type(self) -> None:
        problem_id, commit, materialization = self._materialize()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        job_id = "export-latest-current-native"
        config.export_service.create_export_job(
            job_id=job_id,
            problem_id=problem_id,
            actor_user_id=int(actor["id"]),
            export_type="native",
            source_commit=commit,
        )
        config.export_service.mark_export_job_running(
            job_id,
            source_commit=commit,
        )
        export_id, archive = config.export_service.create_export(
            self.problem,
            "native",
            materialization_id=materialization["id"],
        )
        config.export_service.mark_export_job_succeeded(
            job_id,
            materialization_id=materialization["id"],
            export_id=export_id,
        )

        current = config.export_service.latest_succeeded_export_job(
            problem_id,
            commit,
            "native",
        )
        self.assertIsNotNone(current)
        self.assertEqual(current["id"], job_id)
        self.assertEqual(current["export_id"], export_id)
        self.assertTrue(current["filename"].endswith("-native-v1.zip"))
        self.assertTrue(archive.is_file())
        self.assertIsNone(
            config.export_service.latest_succeeded_export_job(
                problem_id,
                commit,
                "icpc",
            )
        )
        self.assertIsNone(
            config.export_service.latest_succeeded_export_job(
                problem_id,
                "f" * 40,
                "native",
            )
        )

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

    def test_same_revision_reuses_native_and_missing_archive_requires_explicit_rebuild(self) -> None:
        problem_id, _commit, first = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        verification_called = False

        def unexpected_verification(
            _snapshot: Path,
            _commit: str,
            _revision_number: int,
            _verification_id: str,
        ) -> str:
            nonlocal verification_called
            verification_called = True
            raise AssertionError("reused Native must not run verification")

        second = config.problem_package_service.ensure_materialization(
            revision,
            unexpected_verification,
        )
        self.assertEqual(second["id"], first["id"])
        self.assertFalse(verification_called)

        _stored, archive = config.problem_package_service.native_archive(first["id"])
        archive.unlink()
        self.assertIsNone(
            config.problem_package_service.available_materialization(problem_id, revision.source_commit)
        )
        with self.assertRaisesRegex(ValueError, "explicit rebuild"):
            config.problem_package_service.ensure_materialization(
                revision,
                self._verification_builder(problem_id),
            )
        restored = config.problem_package_service.rebuild_materialization(
            revision, self._verification_builder(problem_id)
        )
        self.assertEqual(restored["id"], first["id"])
        self.assertTrue(config.problem_package_service.native_archive(restored["id"])[1].is_file())

    def test_concurrent_materialization_request_fails_fast(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = config.problem_package_service.published_revision(problem_id)
        build_count = 0
        count_lock = threading.Lock()
        build_started = threading.Event()
        release_build = threading.Event()
        delegate = self._verification_builder(problem_id)

        def build(snapshot: Path, commit: str, revision_number: int, verification_id: str) -> str:
            nonlocal build_count
            with count_lock:
                build_count += 1
            build_started.set()
            self.assertTrue(release_build.wait(timeout=5))
            return delegate(snapshot, commit, revision_number, verification_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                config.problem_package_service.ensure_materialization,
                revision,
                build,
            )
            self.assertTrue(build_started.wait(timeout=5))
            second = executor.submit(
                config.problem_package_service.ensure_materialization,
                revision,
                build,
            )
            with self.assertRaisesRegex(RuntimeError, "operation already running"):
                second.result(timeout=5)
            release_build.set()
            materialization = first.result(timeout=5)

        self.assertEqual(build_count, 1)
        self.assertTrue(materialization["id"])

    def test_explicit_rebuild_reuses_identity_and_invalidates_exports(self) -> None:
        problem_id, _commit, first = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        export_id, _archive = config.export_service.create_export(
            self.problem,
            "native",
            materialization_id=first["id"],
        )

        rebuilt = config.problem_package_service.rebuild_materialization(
            revision,
            self._verification_builder(problem_id, answer_bytes=b"changed\n"),
        )

        self.assertEqual(rebuilt["id"], first["id"])
        self.assertIsNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id]))
        with config.problem_package_service.open_reader(rebuilt["id"]) as native:
            answer = native.payload(native.manifest["tests"][0], "answer")
            self.assertIsNotNone(answer)
            self.assertEqual(answer.read_bytes(), b"changed\n")

    def test_inner_integrity_failure_deletes_native_and_export_records(self) -> None:
        problem_id, _commit, materialization = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        export_id, archive = config.export_service.create_export(
            self.problem,
            "native",
            materialization_id=materialization["id"],
        )
        with zipfile.ZipFile(archive, "r") as source:
            entries = [(info, source.read(info.filename)) for info in source.infolist()]
        tampered = archive.with_suffix(".tampered")
        with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info, payload in entries:
                if info.filename == "test_data/tests/001/input":
                    payload = b"tampered\n"
                target.writestr(info, payload)
        tampered.replace(archive)
        payload = archive.read_bytes()
        db_execute(
            """UPDATE problem_package_materializations
               SET archive_sha256=?,archive_size_bytes=? WHERE id=?""",
            [hashlib.sha256(payload).hexdigest(), len(payload), materialization["id"]],
        )

        with self.assertRaisesRegex(ValueError, "integrity check failed"):
            config.problem_package_service.ensure_materialization(
                revision,
                self._verification_builder(problem_id),
            )

        stored = config.problem_package_service.store.materialization(materialization["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "unavailable")
        self.assertFalse(archive.exists())
        self.assertIsNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id]))

    def test_failed_explicit_rebuild_preserves_valid_native_and_exports(self) -> None:
        problem_id, _commit, materialization = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        export_id, original_archive = config.export_service.create_export(
            self.problem,
            "native",
            materialization_id=materialization["id"],
        )
        original_bytes = original_archive.read_bytes()

        def fail_verification(
            _snapshot: Path,
            _commit: str,
            _revision_number: int,
            _verification_id: str,
        ) -> str:
            raise ValueError("forced rebuild failure")

        with self.assertRaisesRegex(ValueError, "forced rebuild failure"):
            config.problem_package_service.rebuild_materialization(
                revision,
                fail_verification,
            )

        stored, archive = config.problem_package_service.native_archive(
            materialization["id"]
        )
        self.assertEqual(stored["status"], "available")
        self.assertEqual(archive.read_bytes(), original_bytes)
        self.assertIsNotNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id]))

    def test_rebuild_publish_failure_restores_valid_native_and_exports(self) -> None:
        problem_id, _commit, materialization = self._materialize()
        revision = config.problem_package_service.published_revision(problem_id)
        export_id, original_archive = config.export_service.create_export(
            self.problem,
            "native",
            materialization_id=materialization["id"],
        )
        original_bytes = original_archive.read_bytes()

        with patch.object(
            config.problem_package_service.store,
            "insert_materialization",
            side_effect=RuntimeError("forced publish failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced publish failure"):
                config.problem_package_service.rebuild_materialization(
                    revision,
                    self._verification_builder(problem_id, answer_bytes=b"changed\n"),
                )

        stored, archive = config.problem_package_service.native_archive(
            materialization["id"]
        )
        self.assertEqual(stored["archive_sha256"], materialization["archive_sha256"])
        self.assertEqual(archive.read_bytes(), original_bytes)
        self.assertIsNotNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id]))

    def test_distinct_git_commits_with_the_same_tree_are_distinct_revisions(self) -> None:
        workspace, problem_id, first_commit = self._publish_problem()
        first_revision = config.problem_package_service.published_revision(problem_id)
        first = config.problem_package_service.ensure_materialization(
            first_revision,
            self._verification_builder(problem_id),
        )

        commit = run_git(
            ["git", "-C", str(workspace), "commit", "--allow-empty", "-m", "publish same tree again"]
        )
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_git(["git", "-C", str(workspace), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)

        second_revision = config.problem_package_service.published_revision(problem_id)
        self.assertNotEqual(second_revision.source_commit, first_commit)
        second = config.problem_package_service.ensure_materialization(
            second_revision,
            self._verification_builder(problem_id),
        )
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["source_digest"], first["source_digest"])
        self.assertEqual(second["revision_number"], first["revision_number"] + 1)

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

    def test_icpc_export_only_annotates_custom_mixed_submissions(self) -> None:
        workspace, problem_id, _commit = self._publish_problem()
        for expected in SUBMISSION_RULES:
            if expected == "accepted":
                continue
            source = workspace / "solutions" / f"{expected}.cpp"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            source.with_name(f"{source.name}.desc").write_text(
                f"expected: {expected}\n",
                encoding="utf-8",
            )
        config.git_service.commit(
            workspace,
            "add expected behavior fixtures",
            self.user,
            f"{self.user}@polygonlike.local",
        )
        config.git_service.push(workspace, "main")
        revision = config.problem_package_service.published_revision(problem_id)
        materialization = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )

        def compile_statement(_snapshot: Path, destination: Path, **_kwargs: object) -> bool:
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "problem.en.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
            return True

        with patch.object(
            config.export_service,
            "_try_compile_statement_pdf",
            side_effect=compile_statement,
        ):
            _export_id, archive = config.export_service.create_export(
                self.problem,
                "icpc",
                materialization_id=materialization["id"],
            )

        mixed = {"tle_or_correct", "tle_or_re", "rejected"}
        with zipfile.ZipFile(archive, "r") as package:
            for expected, rule in SUBMISSION_RULES.items():
                source_name = "accepted.cpp" if expected == "accepted" else f"{expected}.cpp"
                payload = package.read(
                    f"submissions/{rule['directory']}/{source_name}"
                )
                self.assertEqual(
                    b"@EXPECTED_RESULTS@" in payload,
                    expected in mixed,
                    expected,
                )

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

    def test_native_import_rejects_source_only_archives(self) -> None:
        with tempfile.TemporaryDirectory(prefix="native-source-only-") as temp:
            archive = Path(temp) / "source-only.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("config/problem.json", "{}\n")
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(ValueError, "source-only Native packages"):
                NativePackageImportService().import_package(
                    workspace,
                    archive.name,
                    archive.read_bytes(),
                )

    def test_icpc_conversion_rejects_missing_statements(self) -> None:
        workspace, problem_id, _commit = self._publish_problem()
        shutil.rmtree(workspace / "statement-sections")
        config.git_service.commit(
            workspace,
            "remove all statements",
            self.user,
            f"{self.user}@polygonlike.local",
        )
        config.git_service.push(workspace, "main")
        revision = config.problem_package_service.published_revision(problem_id)
        materialization = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        with self.assertRaisesRegex(ValueError, "at least one problem statement"):
            config.export_service.create_export(
                self.problem,
                "icpc",
                materialization_id=materialization["id"],
            )

    def test_icpc_conversion_rejects_invalid_configured_checker(self) -> None:
        workspace, problem_id, _commit = self._publish_problem()
        build_path = workspace / "config" / "build.json"
        build: dict[str, object] = {}
        if build_path.is_file():
            build = json.loads(build_path.read_text(encoding="utf-8"))
        build["checker_source"] = "checkers/missing.cpp"
        build_path.write_text(json.dumps(build, indent=2) + "\n", encoding="utf-8")
        config.git_service.commit(
            workspace,
            "configure missing checker",
            self.user,
            f"{self.user}@polygonlike.local",
        )
        config.git_service.push(workspace, "main")
        revision = config.problem_package_service.published_revision(problem_id)
        materialization = config.problem_package_service.ensure_materialization(
            revision,
            self._verification_builder(problem_id),
        )
        with self.assertRaisesRegex(ValueError, "checker_source is configured but invalid"):
            config.export_service.create_export(
                self.problem,
                "icpc",
                materialization_id=materialization["id"],
            )

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
