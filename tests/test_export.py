import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml

from app.impl.run_export.export import _revision_rows
from app.impl.runtime.dependency import bind_application
from app.main import app, runtime
import app.impl.workspace.context_job as workspace_context_job
from app.service.execution.codec import execution_result_json
from app.service.importing.polygon_replica import PolygonReplicaPackageImportService
from app.service.platform.git_process import run_git
from app.service.problem.build_config import (
    BuildConfig,
    dumps_build_config,
)
from app.service.problem_package.manifest import load_manifest, validate_manifest_files
from app.service.problem_package.service import (
    NativePackageOperationBusy,
)
from app.service.problem_package.store import MaterializationRow
from app.service.problem_package.workflow import (
    build_full_verification_targets,
    build_standard_solution_verification_targets,
)
from tests.archive_support import import_problem_package
from tests.common import E2ETestBase, override_config_values
from tests.db_helpers import (
    admit_test_verification,
    db_execute,
    db_fetch_all,
    db_fetch_one,
)
from tests.ui_support import _request
from tests.execution_result_helpers import execution_result
from tests.package_builders import PdfSandbox
from tests.judgehost_support import JudgehostReply, reporting_judgehost
from tests.package_support import blocked_export_queue, publish_problem, verification_builder


def _archive_payloads(path: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(path, "r") as archive:
        return sorted(
            (info.filename, archive.read(info))
            for info in archive.infolist()
            if not info.is_dir()
        )


class TestNativePackageWorkflow(unittest.TestCase):
    def test_native_package_reserves_the_configured_main_solution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir)
            solutions = source_root / "solutions"
            solutions.mkdir(parents=True)
            for source_name, expected_behavior in (
                ("also_ac.cpp", "accepted"),
                ("official.cpp", "accepted"),
                ("wrong.cpp", "wrong_answer"),
            ):
                source = solutions / source_name
                source.write_text("int main() { return 0; }\n", encoding="utf-8")
                source.with_name(f"{source.name}.desc").write_text(
                    f"expected: {expected_behavior}\n",
                    encoding="utf-8",
                )
            config_path = source_root / "config" / "build.json"
            config_path.parent.mkdir(parents=True)
            config = BuildConfig(generator_sources=[])
            config["accepted_solution_source"] = "solutions/official.cpp"
            config_path.write_text(dumps_build_config(config), encoding="utf-8")

            targets, accepted_source = build_full_verification_targets(source_root)
            standard_targets, standard_source = (
                build_standard_solution_verification_targets(source_root)
            )

        self.assertEqual(accepted_source, "solutions/official.cpp")
        self.assertEqual(
            targets,
            [
                {
                    "path": "solutions/also_ac.cpp",
                    "expected_behavior": "accepted",
                    "program_id": "solution-0",
                },
                {
                    "path": "solutions/official.cpp",
                    "expected_behavior": "accepted",
                    "program_id": "accepted",
                },
                {
                    "path": "solutions/wrong.cpp",
                    "expected_behavior": "wrong_answer",
                    "program_id": "solution-1",
                },
            ],
        )
        self.assertEqual(standard_source, "solutions/official.cpp")
        self.assertEqual(
            standard_targets,
            [
                {
                    "path": "solutions/official.cpp",
                    "expected_behavior": "accepted",
                    "program_id": "accepted",
                }
            ],
        )


class TestPublishedRevisionExport(E2ETestBase):
    def _publish_problem(
        self,
        *,
        test_id: str = "001",
        test_ids: tuple[str, ...] | None = None,
        extra_solutions: dict[str, str] | None = None,
        mode: str = "pass-fail",
    ) -> tuple[Path, int, str]:
        return publish_problem(
            Path(self._workspace_path()),
            self.problem,
            self.user,
            test_id=test_id,
            test_ids=test_ids,
            extra_solutions=extra_solutions,
            mode=mode,
        )

    def _native_package(self) -> tuple[int, str, MaterializationRow]:
        _workspace, problem_id, commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        self.assertEqual(revision.source_commit, commit)
        verified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )
        self.assertRegex(verified["verification_id"], r"^ver-[0-9a-f]+$")
        return problem_id, commit, verified

    def test_standard_solution_workflow_materializes_only_main_correct_evidence(self) -> None:
        _workspace, problem_id, commit = self._publish_problem(
            extra_solutions={"unneeded.cpp": "wrong_answer"},
        )
        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        with reporting_judgehost(
            runtime.judgehost_task_service,
            lambda _work: JudgehostReply(output=b"2\n"),
        ):
            package = runtime.native_package_workflow.ensure(
                revision=runtime.problem_package_service.published_revision(problem_id),
                actor_username=self.user,
                standard_solution_only=True,
            )
        record = runtime.verification_service.verification_record(package["verification_id"])
        self.assertEqual(record["kind"], "package")
        self.assertEqual(record["status"], "ok")
        tasks = db_fetch_all(
            "SELECT task_kind,source_path FROM verification_tasks WHERE verification_id=?",
            [package["verification_id"]],
        )
        self.assertFalse(any(row["task_kind"] == "solution-run" for row in tasks))
        self.assertTrue(any(row["source_path"] == "solutions/accepted.cpp" for row in tasks))
        with runtime.problem_package_service.open_reader(package["id"]) as reader:
            self.assertEqual(reader.manifest["source_commit"], commit)
            answer = reader.payload(reader.manifest["tests"][0], "answer")
            self.assertIsNotNone(answer)
            self.assertEqual(answer.read_bytes(), b"2\n")

    def test_package_revision_statement_links_select_exact_package(self) -> None:
        _problem_id, commit, native_package = self._native_package()
        with bind_application(app):
            rows = _revision_rows(
                _request(f"/problems/{self.problem}/export"),
                self.problem,
                commit,
                [native_package],
                {native_package["id"]: True},
                {native_package["id"]: {}},
            )

        links = rows[0]["statement_preview_links"]
        self.assertEqual(
            [(link["output_kind"], link["label"]) for link in links],
            [
                ("html", "Statements (HTML, English)"),
                ("pdf", "Statements (PDF, English)"),
            ],
        )
        for link in links:
            query = parse_qs(urlparse(link["href"]).query)
            self.assertEqual(query["source"], ["native_package"])
            self.assertEqual(
                query["native_package_id"],
                [native_package["id"]],
            )
            self.assertEqual(query["language"], ["english"])

    def test_package_export_job_stays_queued_until_worker_starts(self) -> None:
        problem_id, commit, native_package = self._native_package()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        with blocked_export_queue():
            started = workspace_context_job.start_export_job(
                runtime,
                self.problem,
                self.user,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="native",
                export_job_id="export-queued-worker-boundary",
            )
            self.assertTrue(started)
            row = db_fetch_one(
                "SELECT status,export_type,started_at FROM export_jobs WHERE id=?",
                ["export-queued-worker-boundary"],
            )
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["export_type"], "native")
            self.assertIsNone(row["started_at"])
        row = db_fetch_one(
            "SELECT status,source_commit,materialization_id,export_id FROM export_jobs WHERE id=?",
            ["export-queued-worker-boundary"],
        )
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["source_commit"], commit)
        self.assertEqual(row["materialization_id"], native_package["id"])
        self.assertIsNone(row["export_id"])
        with runtime.problem_package_service.open_reader(str(row["materialization_id"])) as reader:
            self.assertEqual(reader.manifest["source_commit"], commit)

    def test_same_commit_exports_distinct_formats_but_deduplicates_the_same_format(self) -> None:
        problem_id, _commit, native_package = self._native_package()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ), blocked_export_queue():
            for export_id, package_format, accepted in (
                ("export-first", "domjudge", True),
                ("export-second", "icpc-2025-09", True),
                ("export-duplicate", "domjudge", False),
            ):
                started = workspace_context_job.start_export_job(
                    runtime,
                    self.problem,
                    self.user,
                    actor_user_id=int(actor["id"]),
                    problem_id=problem_id,
                    requested_format=package_format,
                    export_job_id=export_id,
                )
                self.assertEqual(started, accepted)
            self.assertIsNone(db_fetch_one("SELECT id FROM export_jobs WHERE id='export-duplicate'"))
        for export_id, package_format in (
            ("export-first", "domjudge"),
            ("export-second", "icpc-2025-09"),
        ):
            row = db_fetch_one("SELECT status,materialization_id FROM export_jobs WHERE id=?", [export_id])
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["materialization_id"], native_package["id"])
            cached = runtime.export_service.cached_external_package(
                problem_id=problem_id,
                native_package_id=native_package["id"],
                package_format=package_format,
            )
            self.assertIsNotNone(cached)
            with zipfile.ZipFile(cached.path) as package:
                self.assertIn("problem.yaml", package.namelist())

    def test_contest_export_shares_inflight_work_and_consumes_the_frozen_revision(self) -> None:
        problem_id, commit, native_package = self._native_package()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        workspace = Path(self._workspace_path())
        (workspace / "published-later.txt").write_text("new revision\n", encoding="utf-8")
        later_commit = runtime.git_service.commit(workspace, "publish newer revision", self.user, "test@example.org")
        runtime.git_service.push(workspace, "main")
        self.assertNotEqual(later_commit, commit)
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ), blocked_export_queue():
            first_job, first_future = workspace_context_job.start_ready_external_export_job(
                runtime,
                self.problem,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="domjudge",
                source_commit=commit,
                native_package_id=native_package["id"],
                native_archive_sha256=native_package["archive_sha256"],
                export_job_id="export-contest-first",
            )
            second_job, second_future = workspace_context_job.start_ready_external_export_job(
                runtime,
                self.problem,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="domjudge",
                source_commit=commit,
                native_package_id=native_package["id"],
                native_archive_sha256=native_package["archive_sha256"],
                export_job_id="export-contest-second",
            )
            self.assertEqual(second_job, first_job)
            self.assertIsNone(db_fetch_one("SELECT id FROM export_jobs WHERE id='export-contest-second'"))
        self.assertFalse(first_future.is_alive())
        self.assertFalse(second_future.is_alive())
        self.assertIsNone(first_future.exception())
        row = db_fetch_one("SELECT status,source_commit,materialization_id FROM export_jobs WHERE id=?", [first_job])
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["source_commit"], commit)
        self.assertEqual(row["materialization_id"], native_package["id"])
        self.assertIsNone(runtime.problem_package_service.store.materialization_for_revision(problem_id, later_commit))

    def test_native_package_contains_source_payloads_and_statement_build(self) -> None:
        workspace, problem_id, commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )
        (workspace / "dirty-only.txt").write_text(
            "must not be exported\n",
            encoding="utf-8",
        )

        archive = runtime.storage_layout.resolve_artifact(
            verified["archive_rel_path"]
        )
        self.assertEqual(verified["source_commit"], commit)
        with zipfile.ZipFile(archive, "r") as package:
            names = set(package.namelist())
            self.assertIn("config/problem.json", names)
            self.assertIn("statement/statements.ftl", names)
            self.assertIn("statement/problem.tex", names)
            self.assertIn("statement/olymp.sty", names)
            self.assertIn("test-data/manifest.json", names)
            self.assertIn("test-data/tests/001/input", names)
            self.assertIn("test-data/tests/001/answer", names)
            self.assertIn("statement-build/english/statements.tex", names)
            self.assertIn("statement-build/english/problem.tex", names)
            self.assertIn("statement-build/english/examples.tex", names)
            self.assertIn("statement-build/english/olymp.sty", names)
            self.assertIn("statement-build/english/examples/sample-1/display.in", names)
            self.assertIn("statement-build/english/examples/sample-1/display.ans", names)
            self.assertNotIn("dirty-only.txt", names)
            self.assertNotIn("statement/examples.tex", names)
            manifest = json.loads(package.read("test-data/manifest.json"))
            self.assertEqual(
                set(manifest),
                {
                    "mode",
                    "pass_limit",
                    "revision_number",
                    "solutions",
                    "source_commit",
                    "source_digest",
                    "tests",
                },
            )
            self.assertEqual(manifest["source_commit"], commit)
            self.assertEqual(
                manifest["solutions"],
                [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                    }
                ],
            )

    def test_multilanguage_package_extracts_the_selected_offline_statement(self) -> None:
        workspace = Path(self._workspace_path())
        shutil.copytree(
            workspace / "statement-sections" / "english",
            workspace / "statement-sections" / "chinese",
        )
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )
        archive = runtime.storage_layout.artifacts_root / package["archive_rel_path"]
        with zipfile.ZipFile(archive) as bundle:
            english_input = bundle.read("statement-build/english/examples/sample-1/display.in")
            chinese_input = bundle.read("statement-build/chinese/examples/sample-1/display.in")
            expected_tex = bundle.read("statement-build/english/problem.tex")
        self.assertEqual(english_input, b"display input\n")
        self.assertEqual(chinese_input, english_input)
        with tempfile.TemporaryDirectory(prefix="statement-extract-test-") as temp:
            destination = Path(temp) / "render"
            destination.mkdir()
            runtime.problem_package_service.extract_statement_render_tree(
                package["id"], "english", destination,
            )
            self.assertEqual((destination / "problem.tex").read_bytes(), expected_tex)
            self.assertEqual((destination / "examples/sample-1/display.in").read_bytes(), english_input)
            self.assertFalse((destination / "test-data").exists())
            self.assertFalse((destination / "statement-build").exists())

    def test_missing_statement_language_does_not_invalidate_native_package(
        self,
    ) -> None:
        _problem_id, _commit, native_package = self._native_package()
        with tempfile.TemporaryDirectory(prefix="statement-extract-test-") as temp:
            destination = Path(temp) / "render"
            destination.mkdir()
            with self.assertRaisesRegex(
                ValueError,
                "has no chinese statement",
            ):
                runtime.problem_package_service.extract_statement_render_tree(
                    native_package["id"],
                    "chinese",
                    destination,
                )

        current = runtime.problem_package_service.native_package(
            native_package["id"]
        )
        self.assertIsNotNone(current)
        self.assertEqual(current["status"], "available")

    def test_valid_native_package_is_reused_without_verification(self) -> None:
        problem_id, _commit, first = self._native_package()
        revision = runtime.problem_package_service.published_revision(problem_id)


        second = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second, first)

    def test_corrupt_native_package_is_reverified_in_the_same_export_job(self) -> None:
        problem_id, commit, first = self._native_package()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            old_export_id, old_external_package, _warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                native_package_id=first["id"],
            )
        native_archive = runtime.storage_layout.resolve_artifact(
            first["archive_rel_path"]
        )
        native_archive.write_bytes(b"corrupt Native Package")

        override_config_values(self, runtime.config_values, JUDGEHOST_ENABLE=True)
        with (
            reporting_judgehost(runtime.judgehost_task_service, lambda _work: JudgehostReply(output=b"changed\n")),
            patch.object(runtime.tex_compile_service, "sandbox", PdfSandbox()),
            blocked_export_queue(),
        ):
            started = workspace_context_job.start_export_job(
                runtime,
                self.problem,
                self.user,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="icpc-2025-09",
                export_job_id="export-reverify-corrupt",
                standard_solution_only=True,
            )

        self.assertTrue(started)
        job = db_fetch_one(
            """SELECT status,source_commit,materialization_id,export_id
               FROM export_jobs WHERE id=?""",
            ["export-reverify-corrupt"],
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "succeeded")
        self.assertEqual(str(job["source_commit"]), commit)
        self.assertEqual(str(job["materialization_id"]), first["id"])
        self.assertTrue(str(job["export_id"]))
        rebuilt = runtime.problem_package_service.native_package(first["id"])
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["status"], "available")
        self.assertNotEqual(rebuilt["verification_id"], first["verification_id"])
        self.assertNotEqual(rebuilt["archive_sha256"], first["archive_sha256"])
        self.assertIsNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [old_export_id]))
        self.assertFalse(old_external_package.exists())

    def test_domjudge_and_icpc_2025_are_independent_external_packages(self) -> None:
        _problem_id, commit, verified = self._native_package()
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            domjudge_id, domjudge_archive, domjudge_warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                native_package_id=verified["id"],
            )
            icpc_id, icpc_archive, icpc_warning = runtime.export_service.create_export(
                self.problem,
                "icpc-2025-09",
                native_package_id=verified["id"],
            )

        self.assertNotEqual(domjudge_id, icpc_id)
        self.assertEqual(domjudge_warning, "")
        self.assertEqual(icpc_warning, "")
        self.assertIn("-domjudge-v", domjudge_archive.name)
        self.assertIn("-icpc-2025-09-v", icpc_archive.name)
        for archive_path, format_version in (
            (domjudge_archive, "legacy"),
            (icpc_archive, "2025-09"),
        ):
            with self.subTest(package=archive_path.name):
                with zipfile.ZipFile(archive_path, "r") as package:
                    metadata = yaml.safe_load(package.read("problem.yaml"))
                self.assertEqual(metadata["problem_format_version"], format_version)
                if format_version == "2025-09":
                    self.assertEqual(metadata["version"], commit)

        repeated_id, repeated_archive, repeated_warning = runtime.export_service.create_export(
            self.problem,
            "domjudge",
            native_package_id=verified["id"],
        )
        self.assertEqual((repeated_id, repeated_archive), (domjudge_id, domjudge_archive))
        self.assertEqual(repeated_warning, domjudge_warning)
        rows = db_fetch_one(
            """SELECT COUNT(*) AS c FROM exports
               WHERE materialization_id=? AND export_type IN (?,?)""",
            [verified["id"], "domjudge", "icpc-2025-09"],
        )
        self.assertEqual(int(rows["c"]), 2)

    def test_cached_external_package_discards_a_corrupt_archive(self) -> None:
        problem_id, _commit, verified = self._native_package()
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            export_id, archive_path, _warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                native_package_id=verified["id"],
            )
        archive_path.write_bytes(b"corrupt external package")

        cached = runtime.export_service.cached_external_package(
            problem_id=problem_id,
            native_package_id=verified["id"],
            package_format="domjudge",
        )

        self.assertIsNone(cached)
        self.assertIsNone(
            db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id])
        )

    def test_qoj_adapter_publishes_a_root_test_data_archive(self) -> None:
        _problem_id, _commit, verified = self._native_package()
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            export_id, archive_path, warning = (
                runtime.export_service.create_export(
                    self.problem,
                    "qoj",
                    native_package_id=verified["id"],
                )
            )

        self.assertRegex(export_id, r"^e-[0-9a-f]+$")
        self.assertIn("-qoj-v", archive_path.name)
        self.assertEqual(
            warning,
            "QOJ Hack must be disabled until std and val are available.",
        )
        with zipfile.ZipFile(archive_path) as package:
            names = set(package.namelist())
            self.assertTrue(
                {
                    "problem.conf",
                    "statement.pdf",
                    "1.in",
                    "1.ans",
                    "ex_1.in",
                    "ex_1.ans",
                    "std.cpp",
                    "chk.cpp",
                }.issubset(names)
            )
            self.assertFalse(any(name.startswith("package/") for name in names))
            self.assertNotIn("download.zip", names)

    def test_ppf_omits_compile_error_submission_and_reuses_warning_with_cache(
        self,
    ) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            extra_solutions={"rejected.cpp": "compile_error"},
        )
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                solution_verdicts={
                    "solutions/rejected.cpp": ("compile_error", "CE"),
                },
            ),
        )
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            first_id, first_archive, first_warning = (
                runtime.export_service.create_export(
                    self.problem,
                    "icpc-2025-09",
                    native_package_id=verified["id"],
                )
            )
        second_id, second_archive, second_warning = (
            runtime.export_service.create_export(
                self.problem,
                "icpc-2025-09",
                native_package_id=verified["id"],
            )
        )

        self.assertEqual((second_id, second_archive), (first_id, first_archive))
        self.assertEqual(second_warning, first_warning)
        self.assertIn("solutions/rejected.cpp", first_warning)
        with zipfile.ZipFile(first_archive) as package:
            self.assertFalse(
                any(name.endswith("/rejected.cpp") for name in package.namelist())
            )

    def test_package_verification_does_not_require_other_solution_results(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            extra_solutions={"broken.cpp": "rejected"},
        )
        revision = runtime.problem_package_service.published_revision(problem_id)
        native_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )

        self.assertFalse(
            runtime.problem_package_service.native_package_verified(native_package)
        )
        with runtime.problem_package_service.open_reader(native_package["id"]) as reader:
            self.assertEqual(
                reader.manifest["solutions"],
                [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                    },
                    {
                        "source_path": "solutions/broken.cpp",
                        "expected_behavior": "rejected",
                    },
                ],
            )

    def test_full_verification_certifies_existing_package_and_keeps_cached_export(
        self,
    ) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        native_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )
        with patch.object(
            runtime.tex_compile_service, "sandbox", PdfSandbox(),
        ):
            export_id, _export_archive, _warning = (
                runtime.export_service.create_export(
                    self.problem,
                    "domjudge",
                    native_package_id=native_package["id"],
                )
            )
        certified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )

        self.assertEqual(certified["id"], native_package["id"])
        self.assertNotEqual(
            certified["verification_id"],
            native_package["verification_id"],
        )
        self.assertTrue(
            runtime.problem_package_service.native_package_verified(certified)
        )
        cached_export = db_fetch_one(
            "SELECT id,archive_rel_path FROM exports WHERE materialization_id=?",
            [native_package["id"]],
        )
        self.assertIsNotNone(cached_export)
        self.assertEqual(str(cached_export["id"]), export_id)

    def test_full_verification_keeps_existing_package_readable(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        native_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )
        verification_started = threading.Event()
        release_verification = threading.Event()
        completed: list[MaterializationRow] = []
        failures: list[BaseException] = []
        full_builder = verification_builder(problem_id)

        def blocking_builder(
            snapshot: Path,
            commit: str,
            revision_number: int,
            verification_id: str,
        ) -> str:
            verification_started.set()
            if not release_verification.wait(timeout=5):
                raise AssertionError("full Verification was not released")
            return full_builder(
                snapshot,
                commit,
                revision_number,
                verification_id,
            )

        def verify() -> None:
            try:
                completed.append(
                    runtime.problem_package_service.ensure_native_package(
                        revision,
                        blocking_builder,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=verify, daemon=True)
        worker.start()
        self.assertTrue(verification_started.wait(timeout=5))
        try:
            with runtime.problem_package_service.open_reader(
                native_package["id"]
            ) as reader:
                self.assertEqual(reader.native_package["id"], native_package["id"])
            download = runtime.problem_package_service.open_native_package_download(
                native_package["id"]
            )
            try:
                self.assertTrue(download.stream.read(1))
            finally:
                download.close()
            with self.assertRaises(NativePackageOperationBusy):
                runtime.problem_package_service.ensure_native_package(
                    revision,
                    verification_builder(problem_id),
                )
        finally:
            release_verification.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(completed), 1)
        certified = completed[0]
        self.assertEqual(certified["id"], native_package["id"])
        self.assertEqual(
            certified["archive_rel_path"],
            native_package["archive_rel_path"],
        )
        self.assertEqual(
            certified["archive_sha256"],
            native_package["archive_sha256"],
        )
        self.assertEqual(
            certified["archive_size_bytes"],
            native_package["archive_size_bytes"],
        )
        self.assertNotEqual(
            certified["verification_id"],
            native_package["verification_id"],
        )

    def test_archive_publication_waits_for_existing_reader(self) -> None:
        _problem_id, _commit, native_package = self._native_package()
        publication_started = threading.Event()
        publication_entered = threading.Event()
        failures: list[BaseException] = []

        def publish() -> None:
            try:
                publication_started.set()
                with runtime.problem_package_service._archive_publication(
                    int(native_package["problem_id"]),
                    native_package["source_commit"],
                ):
                    publication_entered.set()
            except BaseException as exc:
                failures.append(exc)

        with runtime.problem_package_service.native_package_read_operation(
            native_package["id"]
        ):
            worker = threading.Thread(target=publish, daemon=True)
            worker.start()
            self.assertTrue(publication_started.wait(timeout=5))
            self.assertFalse(publication_entered.is_set())

        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(publication_entered.is_set())

    def test_standard_only_and_full_verification_write_the_same_payloads(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        standard_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )
        archive = runtime.storage_layout.resolve_artifact(
            standard_package["archive_rel_path"]
        )
        standard_payloads = _archive_payloads(archive)
        archive.write_bytes(b"force a rebuild")

        full_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(problem_id),
        )
        rebuilt_archive = runtime.storage_layout.resolve_artifact(
            full_package["archive_rel_path"]
        )

        self.assertTrue(
            runtime.problem_package_service.native_package_verified(full_package)
        )
        self.assertEqual(_archive_payloads(rebuilt_archive), standard_payloads)

    def test_full_verification_evidence_mismatch_keeps_package_uncertified(
        self,
    ) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        native_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "answer differs from the existing Package",
        ):
            runtime.problem_package_service.ensure_native_package(
                revision,
                verification_builder(problem_id, answer_bytes=b"changed\n"),
            )

        current = runtime.problem_package_service.native_package(native_package["id"])
        self.assertIsNotNone(current)
        self.assertEqual(
            current["verification_id"],
            native_package["verification_id"],
        )
        self.assertEqual(current["archive_sha256"], native_package["archive_sha256"])
        self.assertFalse(
            runtime.problem_package_service.native_package_verified(current)
        )

    def test_failed_full_verification_keeps_package_uncertified(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        native_package = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                verification_kind="package",
            ),
        )

        def fail_verification(
            _snapshot: Path,
            commit: str,
            _revision_number: int,
            verification_id: str,
        ) -> str:
            admission = admit_test_verification(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=None,
                signature="native-package-failed-test",
                source_commit=commit,
                kind="all",
            )
            self.assertEqual(admission.outcome, "admitted")
            transition = runtime.verification_service.fail_verification(
                verification_id,
                reason="full Verification failed",
            )
            self.assertEqual(transition.outcome, "transitioned")
            return verification_id

        with self.assertRaisesRegex(
            ValueError,
            "not a successful full Verification",
        ):
            runtime.problem_package_service.ensure_native_package(
                revision,
                fail_verification,
            )

        current = runtime.problem_package_service.native_package(native_package["id"])
        self.assertIsNotNone(current)
        self.assertEqual(
            current["verification_id"],
            native_package["verification_id"],
        )
        self.assertFalse(
            runtime.problem_package_service.native_package_verified(current)
        )

    def test_duplicate_generated_input_copies_owner_into_native_package(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            test_ids=("001", "021"),
            extra_solutions={"wrong.cpp": "wrong_answer"},
        )
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                test_ids=("001", "021"),
                solution_verdicts={
                    "solutions/wrong.cpp": ("wrong_answer", "WA"),
                },
            ),
        )

        with runtime.problem_package_service.open_reader(verified["id"]) as reader:
            tests = {test["id"]: test for test in reader.manifest["tests"]}
            self.assertEqual(set(tests), {"001", "021"})
            for key in ("input", "answer"):
                owner = reader.payload(tests["001"], key)
                duplicate = reader.payload(tests["021"], key)
                self.assertIsNotNone(owner)
                self.assertIsNotNone(duplicate)
                self.assertEqual(duplicate.read_bytes(), owner.read_bytes())
            self.assertEqual(
                reader.manifest["solutions"],
                [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                    },
                    {
                        "source_path": "solutions/wrong.cpp",
                        "expected_behavior": "wrong_answer",
                    },
                ],
            )

    def test_pre_skipped_duplicate_interactive_input_uses_actual_owner(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            test_ids=("101", "102"),
            extra_solutions={"wrong.cpp": "wrong_answer"},
            mode="interactive",
        )
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_native_package(
            revision,
            verification_builder(
                problem_id,
                answer_bytes=None,
                test_ids=("101", "102"),
                solution_verdicts={
                    "solutions/wrong.cpp": ("wrong_answer", "WA"),
                },
                mode="interactive",
                pre_skipped_ordinals=frozenset((2,)),
            ),
        )

        with runtime.problem_package_service.open_reader(verified["id"]) as reader:
            tests = {test["id"]: test for test in reader.manifest["tests"]}
            self.assertEqual(set(tests), {"101", "102"})
            self.assertNotIn("answer", tests["101"])
            self.assertNotIn("answer", tests["102"])
            owner_input = reader.payload(tests["101"], "input")
            duplicate_input = reader.payload(tests["102"], "input")
            self.assertIsNotNone(owner_input)
            self.assertIsNotNone(duplicate_input)
            self.assertEqual(duplicate_input.read_bytes(), owner_input.read_bytes())
            self.assertEqual(
                reader.manifest["solutions"],
                [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                    },
                    {
                        "source_path": "solutions/wrong.cpp",
                        "expected_behavior": "wrong_answer",
                    },
                ],
            )

    def test_invalid_generator_evidence_does_not_publish_a_native_package(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem(test_ids=("001", "002"))
        service = runtime.problem_package_service
        revision = service.published_revision(problem_id)
        builder = verification_builder(problem_id, test_ids=("001", "002"))
        for verdict, final_status, message in (
            ("SK", "done", "owner is missing"),
            ("OK", "done", "multiple owners"),
            ("OK", "", "generated test result is incomplete"),
            ("FL", "done", "generated test result is incomplete"),
        ):
            with self.subTest(verdict=verdict, final_status=final_status):
                def corrupted_evidence(snapshot: Path, commit: str, revision_number: int, verification_id: str) -> str:
                    builder(snapshot, commit, revision_number, verification_id)
                    db_execute(
                        "UPDATE verification_tasks SET result_json=?,final_status=? "
                        "WHERE verification_id=? AND task_kind='generate-input'",
                        [execution_result_json(execution_result(verdict)), final_status, verification_id],
                    )
                    return verification_id

                with self.assertRaisesRegex(ValueError, message):
                    service.ensure_native_package(revision, corrupted_evidence)
                self.assertIsNone(service.store.materialization_for_revision(problem_id, revision.source_commit))
                build = db_fetch_one("SELECT status FROM problem_package_builds WHERE problem_id=?", [problem_id])
                self.assertEqual(build["status"], "failed")

    def test_distinct_commits_with_the_same_tree_have_distinct_native_packages(
        self,
    ) -> None:
        workspace, problem_id, first_commit = self._publish_problem()
        first_revision = runtime.problem_package_service.published_revision(problem_id)
        first = runtime.problem_package_service.ensure_native_package(
            first_revision,
            verification_builder(problem_id),
        )
        commit = run_git(
            [
                "git",
                "-C",
                str(workspace),
                "commit",
                "--allow-empty",
                "-m",
                "publish same tree again",
            ]
        )
        self.assertEqual(commit.returncode, 0, commit.stderr or commit.stdout)
        push = run_git(["git", "-C", str(workspace), "push", "origin", "HEAD:main"])
        self.assertEqual(push.returncode, 0, push.stderr or push.stdout)

        second_revision = runtime.problem_package_service.published_revision(problem_id)
        self.assertNotEqual(second_revision.source_commit, first_commit)
        second = runtime.problem_package_service.ensure_native_package(
            second_revision,
            verification_builder(problem_id),
        )
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["source_digest"], first["source_digest"])
        self.assertEqual(second["revision_number"], first["revision_number"] + 1)

    def test_polygon_replica_package_imports_only_authored_source(self) -> None:
        _problem_id, _commit, verified = self._native_package()
        archive = runtime.storage_layout.resolve_artifact(
            verified["archive_rel_path"]
        )
        with tempfile.TemporaryDirectory(prefix="polygon-replica-import-") as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            import_problem_package(
                PolygonReplicaPackageImportService(),
                workspace,
                archive.name,
                archive.read_bytes(),
            )
            self.assertTrue((workspace / "config" / "problem.json").is_file())
            self.assertTrue((workspace / "statement" / "statements.ftl").is_file())
            self.assertFalse((workspace / "test-data").exists())
            self.assertFalse((workspace / "statement-build").exists())
            self.assertFalse((workspace / "tests" / "answers").exists())

    def test_manifest_validation_detects_extracted_payload_tampering(self) -> None:
        _problem_id, _commit, verified = self._native_package()
        with runtime.problem_package_service.open_reader(verified["id"]) as reader:
            manifest = load_manifest(reader.root / "test-data" / "manifest.json")
            payload = reader.root / "test-data" / "tests" / "001" / "input"
            payload.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ValueError, "integrity"):
                validate_manifest_files(
                    reader.root,
                    manifest,
                    tests_spec_max_bytes=int(runtime.config_values.TEXTAREA_MAX_BYTES),
                    statement_sample_max_bytes=int(
                        runtime.config_values.STATEMENT_SAMPLE_MAX_BYTES
                    ),
                )
        stored = runtime.problem_package_service.native_package(verified["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "available")

    def test_native_package_manifest_rejects_invalid_required_data(self) -> None:
        _problem_id, _commit, native_package = self._native_package()
        with runtime.problem_package_service.open_reader(native_package["id"]) as reader:
            manifest_path = reader.root / "test-data" / "manifest.json"
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            invalid_documents = (
                (
                    "unsupported shape",
                    {
                        key: value
                        for key, value in original.items()
                        if key != "source_digest"
                    },
                ),
                (
                    "revision_number is invalid",
                    {**original, "revision_number": True},
                ),
                (
                    "test entry must be an object",
                    {**original, "tests": ["001"]},
                ),
                (
                    "solution entry has an unsupported shape",
                    {**original, "solutions": ["solutions/accepted.cpp"]},
                ),
                (
                    "payload size is invalid",
                    {
                        **original,
                        "tests": [{
                            **original["tests"][0],
                            "input": {**original["tests"][0]["input"], "size": True},
                        }],
                    },
                ),
                (
                    "Native Package path",
                    {
                        **original,
                        "tests": [
                            {
                                **original["tests"][0],
                                "input": {
                                    **original["tests"][0]["input"],
                                    "path": "../input",
                                },
                            }
                        ],
                    },
                ),
            )
            for message, document in invalid_documents:
                with self.subTest(message=message):
                    manifest_path.write_text(
                        json.dumps(document, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        manifest = load_manifest(manifest_path)
                        validate_manifest_files(
                            reader.root,
                            manifest,
                            tests_spec_max_bytes=int(
                                runtime.config_values.TEXTAREA_MAX_BYTES
                            ),
                            statement_sample_max_bytes=int(
                                runtime.config_values.STATEMENT_SAMPLE_MAX_BYTES
                            ),
                        )

if __name__ == "__main__":
    raise SystemExit("run through the Linux test suite")
