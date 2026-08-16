import json
import tempfile
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from app.main import runtime
import app.impl.workspace.context_job as workspace_context_job
from app.service.importing.polygon_replica import PolygonReplicaPackageImportService
from app.service.platform.git_process import run_git
from app.service.problem.build_config import (
    BuildConfig,
    dumps_build_config,
    load_build_config,
)
from app.service.problem_package.manifest import load_manifest, validate_manifest_files
from app.service.problem_package.service import (
    FrozenVerifiedRevisionMismatch,
    VerifiedRevisionOperationBusy,
)
from app.service.verification.lifecycle import PlannedTask, verification_task_id
from app.service.verification.task_completion import TaskCompletion
from app.service.verification.types import VerificationTaskStatus
from tests.archive_support import import_problem_package
from tests.common import E2ETestBase
from tests.db_helpers import (
    activate_test_verification,
    admit_test_verification,
    db_fetch_one,
    verification_programs_for_tasks,
)
from tests.execution_result_helpers import execution_result


class TestPublishedRevisionExport(E2ETestBase):
    def test_verified_revision_reserves_the_configured_main_solution(self) -> None:
        from app.service.problem_package.workflow import build_full_verification_targets

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

    def _publish_problem(
        self,
        *,
        test_id: str = "001",
        extra_solutions: dict[str, str] | None = None,
    ) -> tuple[Path, int, str]:
        workspace = Path(self._workspace_path())
        (workspace / "tests" / "manual").mkdir(parents=True, exist_ok=True)
        (workspace / "tests" / "manual" / f"{test_id}.in").write_text(
            "1\n",
            encoding="utf-8",
        )
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
        for filename, expected_behavior in (extra_solutions or {}).items():
            source = workspace / "solutions" / filename
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            source.with_name(f"{source.name}.desc").write_text(
                f"expected: {expected_behavior}\n",
                encoding="utf-8",
            )
        build = load_build_config(workspace)
        build["accepted_solution_source"] = "solutions/accepted.cpp"
        (workspace / "config" / "build.json").write_text(
            dumps_build_config(build),
            encoding="utf-8",
        )
        commit = runtime.git_service.commit(
            workspace,
            "publish verified revision fixture",
            self.user,
            f"{self.user}@polygonlike.local",
        )
        runtime.git_service.push(workspace, "main")
        context = runtime.workspace_service.workspace_context(
            self.problem,
            self.user,
            include_recent=False,
        )
        return workspace, int(context["problem"]["id"]), commit

    @staticmethod
    def _verification_builder(
        problem_id: int,
        *,
        input_bytes: bytes = b"1\n",
        answer_bytes: bytes = b"2\n",
        solution_verdicts: dict[str, tuple[str, str]] | None = None,
    ):
        def build(
            _snapshot: Path,
            commit: str,
            _revision_number: int,
            verification_id: str,
        ) -> str:
            build_row = db_fetch_one(
                """SELECT status,phase FROM problem_package_builds
                   WHERE verification_id=?""",
                [verification_id],
            )
            if build_row is None or (
                str(build_row["status"]),
                str(build_row["phase"]),
            ) != ("running", "verification"):
                raise AssertionError("verified revision build phase is not verification")
            admission = admit_test_verification(
                verification_id=verification_id,
                problem_id=problem_id,
                workspace_id=None,
                signature="verified-revision-test",
                source_commit=commit,
                kind="all",
            )
            if admission.outcome != "admitted":
                raise AssertionError(f"unexpected admission outcome: {admission.outcome}")
            input_ref = runtime.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role="input",
                file_name="001.in",
                payload=input_bytes,
            )
            answer_ref = runtime.verification_service.store_verification_blob(
                verification_id=verification_id,
                test_name="001.in",
                role="answer",
                file_name="001.ans",
                payload=answer_bytes,
            )
            task_id = verification_task_id(verification_id, "accepted", "001.in")
            tasks = [
                PlannedTask(
                    task_id=task_id,
                    predecessor_task_id=None,
                    task_kind="main-correct",
                    source_path="solutions/accepted.cpp",
                    program_id="accepted",
                    test_name="001.in",
                    expected_behavior="accepted",
                )
            ]
            completions = [
                TaskCompletion(
                    task_id=task_id,
                    status=VerificationTaskStatus.DONE,
                    run_id="",
                    judgehost_task_id="",
                    result=execution_result("OK"),
                    input_ref=input_ref,
                    answer_ref=answer_ref,
                )
            ]
            for index, (
                source_path,
                (expected_behavior, verdict),
            ) in enumerate((solution_verdicts or {}).items(), start=1):
                program_id = f"solution-{index}"
                solution_task_id = verification_task_id(
                    verification_id,
                    program_id,
                    "001.in",
                )
                tasks.append(
                    PlannedTask(
                        task_id=solution_task_id,
                        predecessor_task_id=task_id,
                        task_kind="solution-run",
                        source_path=source_path,
                        program_id=program_id,
                        test_name="001.in",
                        expected_behavior=expected_behavior,
                    )
                )
                completions.append(
                    TaskCompletion(
                        task_id=solution_task_id,
                        status=VerificationTaskStatus.DONE,
                        run_id="",
                        judgehost_task_id="",
                        result=execution_result(verdict),
                    )
                )
            activation = activate_test_verification(
                verification_id,
                programs=verification_programs_for_tasks(tasks),
                tasks=tasks,
            )
            if activation.outcome != "activated":
                raise AssertionError(f"unexpected activation outcome: {activation.outcome}")
            completion = runtime.verification_task_store.commit_task_completions(
                completions
            )
            if completion.parent_transition != "ok":
                raise AssertionError(
                    "unexpected verification transition: "
                    f"{completion.parent_transition}"
                )
            return verification_id

        return build

    def _verified_revision(self):
        _workspace, problem_id, commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        self.assertEqual(revision.source_commit, commit)
        verified = runtime.problem_package_service.ensure_verified_revision(
            revision,
            self._verification_builder(problem_id),
        )
        self.assertRegex(verified["verification_id"], r"^ver-[0-9a-f]+$")
        return problem_id, commit, verified

    @staticmethod
    def _compile_statement(tex_path: Path) -> SimpleNamespace:
        pdf_path = tex_path.parent / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        return SimpleNamespace(
            proc=SimpleNamespace(returncode=0, stderr="", stdout=""),
            pdf_path=pdf_path,
        )

    def test_package_export_job_stays_queued_until_worker_starts(self) -> None:
        _workspace, problem_id, commit = self._publish_problem()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        captured_runner: list[object] = []

        class FakeWorker:
            alive = True

            def is_alive(self) -> bool:
                return self.alive

        worker = FakeWorker()

        def submit(*, fn, **_kwargs):
            captured_runner.append(fn)
            return worker, True, "queued"

        key = f"{problem_id}:{commit}"
        try:
            with patch.object(
                runtime.worker_queue_service,
                "submit",
                side_effect=submit,
            ):
                started = workspace_context_job.start_export_job(
                    runtime,
                    self.problem,
                    self.user,
                    actor_user_id=int(actor["id"]),
                    problem_id=problem_id,
                    requested_format="domjudge",
                    export_job_id="export-queued-worker-boundary",
                )

            self.assertTrue(started)
            self.assertEqual(len(captured_runner), 1)
            row = db_fetch_one(
                "SELECT status,export_type,started_at FROM export_jobs WHERE id=?",
                ["export-queued-worker-boundary"],
            )
            self.assertIsNotNone(row)
            self.assertEqual(str(row["status"]), "queued")
            self.assertEqual(str(row["export_type"]), "domjudge")
            self.assertIsNone(row["started_at"])
        finally:
            worker.alive = False
            with runtime.export_lock:
                runtime.export_inflight.discard(key)
                runtime.export_workers.discard(worker)

    def test_native_package_job_finishes_with_the_verified_revision(self) -> None:
        problem_id, commit, verified_revision = self._verified_revision()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)

        class CompletedWorker:
            @staticmethod
            def is_alive() -> bool:
                return False

        def submit(*, fn, **_kwargs):
            fn()
            return CompletedWorker(), True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch.object(
                runtime.verified_revision_workflow,
                "ensure",
                return_value=verified_revision,
            ),
            patch.object(runtime.export_service, "create_export") as create_export,
        ):
            started = workspace_context_job.start_export_job(
                runtime,
                self.problem,
                self.user,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="native",
                export_job_id="export-native-verified-revision",
            )

        self.assertTrue(started)
        row = db_fetch_one(
            """SELECT status,source_commit,materialization_id,export_id
               FROM export_jobs WHERE id=?""",
            ["export-native-verified-revision"],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "succeeded")
        self.assertEqual(str(row["source_commit"]), commit)
        self.assertEqual(str(row["materialization_id"]), verified_revision["id"])
        self.assertIsNone(row["export_id"])
        create_export.assert_not_called()

    def test_same_published_commit_package_exports_fail_fast(self) -> None:
        _workspace, problem_id, commit = self._publish_problem()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        captured_runner: list[object] = []

        class FakeWorker:
            def is_alive(self) -> bool:
                return True

        worker = FakeWorker()

        def submit(*, fn, **_kwargs):
            captured_runner.append(fn)
            return worker, True, "queued"

        key = f"{problem_id}:{commit}"
        try:
            with patch.object(runtime.worker_queue_service, "submit", side_effect=submit):
                first = workspace_context_job.start_export_job(
                    runtime,
                    self.problem,
                    self.user,
                    actor_user_id=int(actor["id"]),
                    problem_id=problem_id,
                    requested_format="domjudge",
                    export_job_id="export-first",
                )
                second = workspace_context_job.start_export_job(
                    runtime,
                    self.problem,
                    self.user,
                    actor_user_id=int(actor["id"]),
                    problem_id=problem_id,
                    requested_format="icpc-2025-09",
                    export_job_id="export-second",
                )

            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(len(captured_runner), 1)
            self.assertIsNone(
                db_fetch_one("SELECT id FROM export_jobs WHERE id='export-second'")
            )
        finally:
            with runtime.export_lock:
                runtime.export_inflight.discard(key)
                runtime.export_workers.discard(worker)

    def test_verified_revision_contains_source_payloads_and_statement_build(self) -> None:
        workspace, problem_id, commit = self._publish_problem()
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_verified_revision(
            revision,
            self._verification_builder(problem_id),
        )
        (workspace / "dirty-only.txt").write_text(
            "must not be exported\n",
            encoding="utf-8",
        )

        stored, archive = runtime.problem_package_service.verified_revision_archive(
            verified["id"]
        )
        self.assertEqual(stored["source_commit"], commit)
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
            self.assertIn("statement-build/english/sample.001.in", names)
            self.assertIn("statement-build/english/sample.001.ans", names)
            self.assertNotIn("source/config/problem.json", names)
            self.assertNotIn("dirty-only.txt", names)
            self.assertNotIn("statement/examples.tex", names)
            rendered_main = package.read(
                "statement-build/english/statements.tex"
            ).decode("utf-8")
            self.assertIn("\\input{problem.tex}", rendered_main)
            manifest = json.loads(package.read("test-data/manifest.json"))
            self.assertEqual(manifest["source_commit"], commit)
            self.assertEqual(manifest["verification"]["id"], verified["verification_id"])
            self.assertEqual(
                manifest["solutions"],
                [
                    {
                        "source_path": "solutions/accepted.cpp",
                        "expected_behavior": "accepted",
                        "verdicts": ["AC"],
                    }
                ],
            )

    def test_valid_verified_revision_is_reused_without_verification(self) -> None:
        problem_id, _commit, first = self._verified_revision()
        revision = runtime.problem_package_service.published_revision(problem_id)

        def unexpected_verification(*_args, **_kwargs):
            raise AssertionError("a valid verified revision must be reused")

        second = runtime.problem_package_service.ensure_verified_revision(
            revision,
            unexpected_verification,
        )

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["archive_sha256"], first["archive_sha256"])

    def test_corrupt_verified_revision_is_reverified_in_the_same_export_job(self) -> None:
        problem_id, commit, first = self._verified_revision()
        actor = db_fetch_one("SELECT id FROM users WHERE username=?", [self.user])
        self.assertIsNotNone(actor)
        with patch.object(
            runtime.tex_compile_service,
            "compile_pdf",
            side_effect=self._compile_statement,
        ):
            old_export_id, old_projection, _warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                verified_revision_id=first["id"],
            )
        _stored, verified_archive = (
            runtime.problem_package_service.verified_revision_archive(first["id"])
        )
        verified_archive.write_bytes(b"corrupt verified revision")

        def ensure(*, revision, **_kwargs):
            return runtime.problem_package_service.ensure_verified_revision(
                revision,
                self._verification_builder(problem_id, answer_bytes=b"changed\n"),
            )

        class CompletedWorker:
            def is_alive(self) -> bool:
                return False

        def submit(*, fn, **_kwargs):
            fn()
            return CompletedWorker(), True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch.object(runtime.verified_revision_workflow, "ensure", side_effect=ensure),
            patch.object(
                runtime.tex_compile_service,
                "compile_pdf",
                side_effect=self._compile_statement,
            ),
        ):
            started = workspace_context_job.start_export_job(
                runtime,
                self.problem,
                self.user,
                actor_user_id=int(actor["id"]),
                problem_id=problem_id,
                requested_format="icpc-2025-09",
                export_job_id="export-reverify-corrupt",
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
        rebuilt = runtime.problem_package_service.verified_revision(first["id"])
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["status"], "available")
        self.assertNotEqual(rebuilt["verification_id"], first["verification_id"])
        self.assertNotEqual(rebuilt["archive_sha256"], first["archive_sha256"])
        self.assertIsNone(db_fetch_one("SELECT id FROM exports WHERE id=?", [old_export_id]))
        self.assertFalse(old_projection.exists())

    def test_domjudge_and_icpc_2025_are_independent_projections(self) -> None:
        _problem_id, commit, verified = self._verified_revision()
        with patch.object(
            runtime.tex_compile_service,
            "compile_pdf",
            side_effect=self._compile_statement,
        ):
            domjudge_id, domjudge_archive, domjudge_warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                verified_revision_id=verified["id"],
            )
            icpc_id, icpc_archive, icpc_warning = runtime.export_service.create_export(
                self.problem,
                "icpc-2025-09",
                verified_revision_id=verified["id"],
            )

        self.assertNotEqual(domjudge_id, icpc_id)
        self.assertEqual(domjudge_warning, "")
        self.assertEqual(icpc_warning, "")
        self.assertIn("-domjudge-v", domjudge_archive.name)
        self.assertIn("-icpc-2025-09-v", icpc_archive.name)
        with zipfile.ZipFile(domjudge_archive, "r") as package:
            names = set(package.namelist())
            self.assertIn("domjudge-problem.ini", names)
            self.assertIn("problem_statement/problem.pdf", names)
            self.assertNotIn("submissions/submissions.yaml", names)
            self.assertFalse(any(name.startswith("statement/") for name in names))
            metadata = yaml.safe_load(package.read("problem.yaml"))
            self.assertNotEqual(metadata.get("problem_format_version"), "2025-09")
        with zipfile.ZipFile(icpc_archive, "r") as package:
            names = set(package.namelist())
            self.assertNotIn("domjudge-problem.ini", names)
            self.assertNotIn("problem_statement/problem.pdf", names)
            self.assertIn("statement/problem.en.pdf", names)
            self.assertIn("submissions/submissions.yaml", names)
            metadata = yaml.safe_load(package.read("problem.yaml"))
            self.assertEqual(metadata["problem_format_version"], "2025-09")
            self.assertEqual(metadata["version"], commit)

        repeated_id, repeated_archive, repeated_warning = runtime.export_service.create_export(
            self.problem,
            "domjudge",
            verified_revision_id=verified["id"],
        )
        self.assertEqual((repeated_id, repeated_archive), (domjudge_id, domjudge_archive))
        self.assertEqual(repeated_warning, domjudge_warning)
        rows = db_fetch_one(
            """SELECT COUNT(*) AS c FROM exports
               WHERE materialization_id=? AND export_type IN (?,?)""",
            [verified["id"], "domjudge", "icpc-2025-09"],
        )
        self.assertEqual(int(rows["c"]), 2)

    def test_ppf_omits_compile_error_submission_and_reuses_warning_with_cache(
        self,
    ) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            extra_solutions={"rejected.cpp": "rejected"},
        )
        revision = runtime.problem_package_service.published_revision(problem_id)
        verified = runtime.problem_package_service.ensure_verified_revision(
            revision,
            self._verification_builder(
                problem_id,
                solution_verdicts={
                    "solutions/rejected.cpp": ("rejected", "CE"),
                },
            ),
        )
        with patch.object(
            runtime.tex_compile_service,
            "compile_pdf",
            side_effect=self._compile_statement,
        ):
            first_id, first_archive, first_warning = (
                runtime.export_service.create_export(
                    self.problem,
                    "icpc-2025-09",
                    verified_revision_id=verified["id"],
                )
            )
        second_id, second_archive, second_warning = (
            runtime.export_service.create_export(
                self.problem,
                "icpc-2025-09",
                verified_revision_id=verified["id"],
            )
        )

        self.assertEqual((second_id, second_archive), (first_id, first_archive))
        self.assertEqual(second_warning, first_warning)
        self.assertIn("solutions/rejected.cpp", first_warning)
        with zipfile.ZipFile(first_archive) as package:
            self.assertFalse(
                any(name.endswith("/rejected.cpp") for name in package.namelist())
            )

    def test_incomplete_solution_verdict_prevents_verified_revision(self) -> None:
        _workspace, problem_id, _commit = self._publish_problem(
            extra_solutions={"broken.cpp": "rejected"},
        )
        revision = runtime.problem_package_service.published_revision(problem_id)

        with self.assertRaisesRegex(
            ValueError,
            "verification solution result is not a complete verdict",
        ):
            runtime.problem_package_service.ensure_verified_revision(
                revision,
                self._verification_builder(
                    problem_id,
                    solution_verdicts={
                        "solutions/broken.cpp": ("rejected", "SK"),
                    },
                ),
            )

    def test_export_publication_holds_verified_revision_operation(self) -> None:
        _problem_id, _commit, verified = self._verified_revision()
        original_insert = runtime.export_service._store.insert_export_record
        competing_errors: list[Exception] = []

        def insert_while_rebuild_competes(**kwargs) -> None:
            def acquire_rebuild_operation() -> None:
                try:
                    with runtime.problem_package_service.verified_revision_operation(
                        verified["id"]
                    ):
                        pass
                except Exception as exc:  # capture the competing thread result
                    competing_errors.append(exc)

            competitor = threading.Thread(
                target=acquire_rebuild_operation,
                daemon=True,
            )
            competitor.start()
            competitor.join(timeout=5)
            self.assertFalse(competitor.is_alive())
            self.assertEqual(len(competing_errors), 1)
            self.assertIsInstance(
                competing_errors[0],
                VerifiedRevisionOperationBusy,
            )
            original_insert(**kwargs)

        with (
            patch.object(
                runtime.export_service._store,
                "insert_export_record",
                side_effect=insert_while_rebuild_competes,
            ),
            patch.object(
            runtime.tex_compile_service,
                "compile_pdf",
                side_effect=self._compile_statement,
            ),
        ):
            export_id, archive, _warning = runtime.export_service.create_export(
                self.problem,
                "domjudge",
                verified_revision_id=verified["id"],
            )

        self.assertTrue(archive.is_file())
        self.assertIsNotNone(
            db_fetch_one("SELECT id FROM exports WHERE id=?", [export_id])
        )

    def test_distinct_commits_with_the_same_tree_have_distinct_verified_revisions(
        self,
    ) -> None:
        workspace, problem_id, first_commit = self._publish_problem()
        first_revision = runtime.problem_package_service.published_revision(problem_id)
        first = runtime.problem_package_service.ensure_verified_revision(
            first_revision,
            self._verification_builder(problem_id),
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
        second = runtime.problem_package_service.ensure_verified_revision(
            second_revision,
            self._verification_builder(problem_id),
        )
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["source_digest"], first["source_digest"])
        self.assertEqual(second["revision_number"], first["revision_number"] + 1)

    def test_polygon_replica_package_imports_only_authored_source(self) -> None:
        _problem_id, _commit, verified = self._verified_revision()
        _stored, archive = runtime.problem_package_service.verified_revision_archive(
            verified["id"]
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

    def test_verified_revision_reader_detects_extracted_payload_tampering(self) -> None:
        _problem_id, _commit, verified = self._verified_revision()
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
        stored = runtime.problem_package_service.verified_revision(verified["id"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "available")

    def test_verified_revision_reader_detects_archive_change_during_read(self) -> None:
        _problem_id, _commit, verified = self._verified_revision()
        _stored, archive = runtime.problem_package_service.verified_revision_archive(
            verified["id"]
        )
        original = archive.read_bytes()
        try:
            with self.assertRaisesRegex(
                FrozenVerifiedRevisionMismatch,
                "archive changed while it was being read",
            ):
                with runtime.problem_package_service.open_reader(verified["id"]):
                    archive.write_bytes(original + b"changed during reader lifetime")
        finally:
            archive.write_bytes(original)

        with runtime.problem_package_service.open_reader(verified["id"]):
            pass


if __name__ == "__main__":
    raise SystemExit("run through the Linux test suite")
