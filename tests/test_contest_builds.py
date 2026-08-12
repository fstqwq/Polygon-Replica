from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import zipfile

from tests.contest_support import ContestActionBase
from tests.db_helpers import (
    db_connection,
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
)
from tests.ui_support import runtime, contest_packages_build_start, uuid


class TestContestBuilds(ContestActionBase):
    def _contest_with_problem(self) -> tuple[str, int, int, str]:
        contest_slug, contest_id, actor_user_id = self.create_contest("revision-build")
        _contest_problem_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "revision-build-problem",
        )
        workspace = Path(runtime.workspace_service.ensure_workspace(problem_slug, "alice"))
        marker = workspace / "notes" / "published.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("published contest fixture\n", encoding="utf-8")
        runtime.git_service.commit(
            workspace,
            "Publish contest build fixture",
            "alice",
            "alice@example.test",
        )
        runtime.git_service.push(workspace, "main")
        return contest_slug, contest_id, problem_id, problem_slug

    def _seed_materialization(
        self,
        *,
        problem_id: int,
        source_commit: str,
        revision_number: int,
    ) -> str:
        materialization_id = f"pm-{uuid.uuid4().hex}"
        archive = (
            Path(runtime.settings.artifacts_root)
            / "materializations"
            / str(problem_id)
            / source_commit
            / "native.zip"
        )
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(f"native:{source_commit}".encode("ascii"))
        payload = archive.read_bytes()
        now = "2026-08-08T00:00:00+00:00"
        db_execute(
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,
                verification_id,status,created_at,checked_at,unavailable_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,'available',?,?,'')
            """,
            [
                materialization_id,
                problem_id,
                source_commit,
                revision_number,
                "0" * 64,
                archive.relative_to(runtime.settings.artifacts_root).as_posix(),
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                f"ver-{uuid.uuid4().int & ((1 << 63) - 1):x}",
                now,
                now,
            ],
        )
        return materialization_id

    def _frozen_revisions(
        self,
        contest_id: int,
        *,
        source_commit: str,
        revision_number: int,
    ) -> list[dict[str, object]]:
        rows = runtime.contest_service.contest_problems(contest_id)
        return [
            {
                "contest_problem_id": int(row["contest_problem_id"]),
                "position": int(row["position"]),
                "label": str(row["idx"]),
                "problem_id": int(row["problem_id"]),
                "statement_folder": str(row["statement_folder"]),
                "problem_slug": str(row["problem_slug"]),
                "source_commit": source_commit,
                "revision_number": revision_number,
            }
            for row in rows
        ]

    def test_build_freezes_current_revision_without_requiring_native(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        published = runtime.problem_package_service.published_revision(problem_id)
        runners: list[Callable[[], None]] = []

        def submit(*, fn, **_kwargs):
            runners.append(fn)
            return None, True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch.object(runtime.export_service, "create_export") as create_export,
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["icpc_bundle"],
            )

        self.assertEqual(response.status_code, 303)
        job_id = parse_qs(urlparse(str(response.headers["location"])).query)["job_id"][0]
        job = db_fetch_one("SELECT status FROM contest_jobs WHERE id=?", [job_id])
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "queued")
        self.assertFalse(
            (
                runtime.contest_service.job_root(
                    contest_slug,
                    job_id,
                    create=False,
                )
                / "contest-sources"
            ).exists()
        )
        item = db_fetch_one(
            """SELECT source_commit,revision_number,materialization_id,archive_sha256
               FROM contest_build_items WHERE job_id=?""",
            [job_id],
        )
        self.assertIsNotNone(item)
        self.assertEqual(str(item["source_commit"]), published.source_commit)
        self.assertEqual(int(item["revision_number"]), published.revision_number)
        self.assertIsNone(item["materialization_id"])
        self.assertIsNone(item["archive_sha256"])
        create_export.assert_not_called()

        def assert_worker_boundary(**_kwargs) -> None:
            running = db_fetch_one("SELECT status FROM contest_jobs WHERE id=?", [job_id])
            self.assertIsNotNone(running)
            self.assertEqual(str(running["status"]), "running")
            raise RuntimeError("stop after worker boundary")

        self.assertEqual(len(runners), 1)
        with (
            patch.object(
                runtime.contest_snapshot_service, "create",
                side_effect=assert_worker_boundary,
            ),
            self.assertRaisesRegex(RuntimeError, "stop after worker boundary"),
        ):
            runners[0]()
        terminal = db_fetch_one("SELECT status FROM contest_jobs WHERE id=?", [job_id])
        self.assertIsNotNone(terminal)
        self.assertEqual(str(terminal["status"]), "failed")

    def test_queue_rejection_fails_job_without_snapshotting_sources(self) -> None:
        contest_slug, contest_id, _problem_id, _problem_slug = self._contest_with_problem()

        with patch.object(
            runtime.worker_queue_service,
            "submit",
            return_value=(None, False, "capacity"),
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["icpc_bundle"],
            )

        self.assertEqual(response.status_code, 303)
        job = db_fetch_one(
            """SELECT id,status,finished_at FROM contest_jobs
               WHERE contest_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            [contest_id],
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "failed")
        self.assertTrue(str(job["finished_at"] or ""))
        self.assertFalse(
            (
                runtime.contest_service.job_root(
                    contest_slug,
                    str(job["id"]),
                    create=False,
                )
                / "contest-sources"
            ).exists()
        )

    def test_requested_outputs_share_one_frozen_revision_mapping(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        published = runtime.problem_package_service.published_revision(problem_id)
        source_commit = published.source_commit
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit=source_commit,
            revision_number=published.revision_number,
        )
        materialization = runtime.problem_package_service.store.materialization(
            materialization_id
        )
        self.assertIsNotNone(materialization)

        def submit(*, fn, **_kwargs):
            fn()
            return None, True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch(
                "app.impl.workspace.published_materialization.ensure_published_materialization",
                return_value=materialization,
            ),
            patch.object(
                runtime.contest_statement_service, "build_pdf",
                return_value={"artifact_id": "ca-pdf", "error": ""},
            ) as build_pdf,
            patch.object(
                runtime.contest_package_service, "build_bundle",
                return_value={"artifact_id": "ca-icpc", "error": ""},
            ) as build_icpc,
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["statement_pdf", "icpc_bundle"],
                language="english",
            )

        self.assertEqual(response.status_code, 303)
        job_id = parse_qs(urlparse(str(response.headers["location"])).query)["job_id"][0]
        job = db_fetch_one(
            "SELECT status,source_generation FROM contest_jobs WHERE id=? AND contest_id=?",
            [job_id, contest_id],
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "ok")
        items = db_fetch_all(
            """
            SELECT source_commit,revision_number,materialization_id,archive_sha256
            FROM contest_build_items WHERE job_id=?
            """,
            [job_id],
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(str(items[0]["source_commit"]), source_commit)
        self.assertEqual(int(items[0]["revision_number"]), published.revision_number)
        self.assertEqual(str(items[0]["materialization_id"]), materialization_id)
        self.assertTrue(str(items[0]["archive_sha256"]))
        build_pdf.assert_called_once()
        build_icpc.assert_called_once()
        self.assertEqual(build_pdf.call_args.kwargs["job_id"], job_id)
        self.assertEqual(build_icpc.call_args.kwargs["job_id"], job_id)
        summary = read_contest_job_summary(contest_id, job_id)
        self.assertEqual(summary["successful_outputs"], ["statement_pdf", "icpc_bundle"])

    def test_build_materializes_current_revision_instead_of_reusing_older_native(self) -> None:
        contest_slug, contest_id, problem_id, problem_slug = self._contest_with_problem()
        older = runtime.problem_package_service.published_revision(problem_id)
        older_materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit=older.source_commit,
            revision_number=older.revision_number,
        )
        workspace = Path(runtime.workspace_service.ensure_workspace(problem_slug, "alice"))
        marker = workspace / "notes" / "newer.txt"
        marker.write_text("new published revision\n", encoding="utf-8")
        runtime.git_service.commit(
            workspace,
            "Publish newer contest revision",
            "alice",
            "alice@example.test",
        )
        runtime.git_service.push(workspace, "main")
        current = runtime.problem_package_service.published_revision(problem_id)
        self.assertNotEqual(current.source_commit, older.source_commit)

        observed_commits: list[str] = []

        def materialize(*, revision, **_kwargs):
            observed_commits.append(revision.source_commit)
            materialization_id = self._seed_materialization(
                problem_id=problem_id,
                source_commit=revision.source_commit,
                revision_number=revision.revision_number,
            )
            row = runtime.problem_package_service.store.materialization(
                materialization_id
            )
            assert row is not None
            return row

        def submit(*, fn, **_kwargs):
            fn()
            return None, True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch(
                "app.impl.workspace.published_materialization.ensure_published_materialization",
                side_effect=materialize,
            ),
            patch.object(
                runtime.contest_package_service, "build_bundle",
                return_value={"artifact_id": "ca-icpc", "error": ""},
            ),
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["icpc_bundle"],
            )

        job_id = parse_qs(urlparse(str(response.headers["location"])).query)[
            "job_id"
        ][0]
        item = db_fetch_one(
            """SELECT source_commit,revision_number,materialization_id
               FROM contest_build_items WHERE job_id=?""",
            [job_id],
        )
        self.assertIsNotNone(item)
        self.assertEqual(observed_commits, [current.source_commit])
        self.assertEqual(str(item["source_commit"]), current.source_commit)
        self.assertEqual(int(item["revision_number"]), current.revision_number)
        self.assertNotEqual(str(item["materialization_id"]), older_materialization_id)

    def test_build_freeze_rejects_changed_roster_without_creating_job(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        published = runtime.problem_package_service.published_revision(problem_id)
        revisions = self._frozen_revisions(
            contest_id,
            source_commit=published.source_commit,
            revision_number=published.revision_number,
        )
        db_execute(
            "UPDATE contest_problems SET label='Z' WHERE contest_id=?",
            [contest_id],
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)

        frozen = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary={"job_type": "build", "contest_slug": contest_slug},
            revisions=revisions,
        )

        self.assertEqual(frozen["outcome"], "roster_changed")
        jobs = db_fetch_all(
            "SELECT id FROM contest_jobs WHERE contest_id=? AND job_type='build'",
            [contest_id],
        )
        self.assertEqual(jobs, [])

    def test_build_freeze_atomically_reuses_active_job(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="c" * 40,
            revision_number=3,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        summary = {
            "job_type": "build",
            "contest_slug": contest_slug,
            "status": "running",
        }

        first = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary=summary,
            revisions=self._frozen_revisions(
                contest_id,
                source_commit="c" * 40,
                revision_number=3,
            ),
        )
        second = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary=summary,
            revisions=self._frozen_revisions(
                contest_id,
                source_commit="c" * 40,
                revision_number=3,
            ),
        )

        self.assertEqual(first["outcome"], "created")
        self.assertEqual(second["outcome"], "already_running")
        self.assertEqual(second["job_id"], first["job_id"])
        rows = db_fetch_all(
            "SELECT id FROM contest_jobs WHERE contest_id=? AND job_type='build'",
            [contest_id],
        )
        self.assertEqual(len(rows), 1)

    def test_build_freeze_fails_immediately_when_sqlite_admission_is_busy(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        published = runtime.problem_package_service.published_revision(problem_id)
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)

        with db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            frozen = runtime.contest_service.freeze_build_job(
                contest_id=contest_id,
                actor_user_id=int(actor["id"]),
                job_type="build",
                summary={"job_type": "build", "contest_slug": contest_slug},
                revisions=self._frozen_revisions(
                    contest_id,
                    source_commit=published.source_commit,
                    revision_number=published.revision_number,
                ),
            )

        self.assertEqual(frozen["outcome"], "busy")
        self.assertEqual(frozen["job_id"], "")

    def test_package_worker_rejects_changed_frozen_native_sha(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="d" * 40,
            revision_number=4,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        frozen = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary={"job_type": "build", "contest_slug": contest_slug},
            revisions=self._frozen_revisions(
                contest_id,
                source_commit="d" * 40,
                revision_number=4,
            ),
        )
        db_execute(
            "UPDATE contest_build_items SET archive_sha256=? WHERE job_id=?",
            ["f" * 64, frozen["job_id"]],
        )

        result = runtime.contest_package_service.build_bundle(
            contest_id=contest_id,
            contest_slug=contest_slug,
            job_id=frozen["job_id"],
        )

        self.assertEqual(result["totals"]["failed"], 1)
        error = str(result["results"][0]["error"])
        self.assertIn("frozen Native archive checksum", error)
        row = db_fetch_one(
            "SELECT status FROM problem_package_materializations WHERE problem_id=?",
            [problem_id],
        )
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"]), "available")

    def test_package_worker_rewrites_contest_owned_short_name(self) -> None:
        contest_slug, contest_id, problem_id, problem_slug = (
            self._contest_with_problem()
        )
        published = runtime.problem_package_service.published_revision(problem_id)
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit=published.source_commit,
            revision_number=published.revision_number,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        frozen = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary={"job_type": "build", "contest_slug": contest_slug},
            revisions=self._frozen_revisions(
                contest_id,
                source_commit=published.source_commit,
                revision_number=published.revision_number,
            ),
        )
        canonical = Path(runtime.settings.artifacts_root) / "canonical-icpc.zip"
        with zipfile.ZipFile(canonical, "w") as archive:
            archive.writestr("problem.yaml", "name: {en: Sample}\n")
            archive.writestr(
                "domjudge-problem.ini",
                "name = Sample\nshort-name = revision-build-problem\n",
            )
        canonical_digest = hashlib.sha256(canonical.read_bytes()).hexdigest()

        materialization = runtime.problem_package_service.store.materialization(
            materialization_id
        )
        self.assertIsNotNone(materialization)

        def canonical_export(
            requested_problem: str,
            requested_type: str,
            *,
            materialization_id: str,
            expected_archive_sha256: str | None = None,
        ) -> tuple[str, Path]:
            self.assertEqual(requested_problem, problem_slug)
            self.assertEqual(requested_type, "icpc")
            self.assertEqual(materialization_id, str(materialization["id"]))
            self.assertEqual(
                expected_archive_sha256,
                str(materialization["archive_sha256"]),
            )
            return "export-canonical", canonical

        with patch.object(
            runtime.export_service,
            "create_export",
            side_effect=canonical_export,
        ):
            result = runtime.contest_package_service.build_bundle(
                contest_id=contest_id,
                contest_slug=contest_slug,
                job_id=frozen["job_id"],
            )

        self.assertEqual(result["totals"]["success"], 1)
        package_file = str(result["results"][0]["package_file"])
        target = runtime.contest_service.job_root(
            contest_slug,
            frozen["job_id"],
        ) / package_file
        with zipfile.ZipFile(target, "r") as archive:
            self.assertIn(
                "short-name = A\n",
                archive.read("domjudge-problem.ini").decode("utf-8"),
            )
        self.assertEqual(
            hashlib.sha256(canonical.read_bytes()).hexdigest(),
            canonical_digest,
        )

    def test_binding_rejects_replacement_of_an_already_frozen_native(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="e" * 40,
            revision_number=5,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        frozen = runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary={"job_type": "build", "contest_slug": contest_slug},
            revisions=self._frozen_revisions(
                contest_id,
                source_commit="e" * 40,
                revision_number=5,
            ),
        )
        replacement_sha = "a" * 64
        db_execute(
            "UPDATE problem_package_materializations SET archive_sha256=? WHERE id=?",
            [replacement_sha, materialization_id],
        )

        with self.assertRaisesRegex(ValueError, "frozen value"):
            runtime.contest_service.bind_build_item_materialization(
                job_id=frozen["job_id"],
                contest_problem_id=int(
                    self._frozen_revisions(
                        contest_id,
                        source_commit="e" * 40,
                        revision_number=5,
                    )[0]["contest_problem_id"]
                ),
                problem_id=problem_id,
                source_commit="e" * 40,
                materialization_id=materialization_id,
                archive_sha256=replacement_sha,
            )

        item = db_fetch_one(
            "SELECT archive_sha256 FROM contest_build_items WHERE job_id=?",
            [frozen["job_id"]],
        )
        self.assertIsNotNone(item)
        self.assertNotEqual(str(item["archive_sha256"]), replacement_sha)

    def test_partial_output_does_not_change_the_frozen_revision(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        published = runtime.problem_package_service.published_revision(problem_id)
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit=published.source_commit,
            revision_number=published.revision_number,
        )
        materialization = runtime.problem_package_service.store.materialization(
            materialization_id
        )
        self.assertIsNotNone(materialization)

        def submit(*, fn, **_kwargs):
            fn()
            return None, True, "queued"

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch(
                "app.impl.workspace.published_materialization.ensure_published_materialization",
                return_value=materialization,
            ),
            patch.object(
                runtime.contest_statement_service, "build_pdf",
                return_value={"artifact_id": "ca-pdf", "error": ""},
            ),
            patch.object(
                runtime.contest_package_service, "build_bundle",
                return_value={"artifact_id": "", "error": "conversion failed"},
            ),
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["statement_pdf", "icpc_bundle"],
            )

        job_id = parse_qs(urlparse(str(response.headers["location"])).query)["job_id"][0]
        job = db_fetch_one("SELECT status FROM contest_jobs WHERE id=?", [job_id])
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "partial")
        frozen = db_fetch_one(
            "SELECT materialization_id FROM contest_build_items WHERE job_id=?",
            [job_id],
        )
        self.assertIsNotNone(frozen)
        self.assertEqual(str(frozen["materialization_id"]), materialization_id)
