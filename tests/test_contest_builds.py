from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests.contest_support import ContestActionBase
from tests.db_helpers import db_execute, db_fetch_all, db_fetch_one, read_contest_job_summary
from tests.ui_support import config, contest_packages_build_start, uuid


class TestContestBuilds(ContestActionBase):
    def _contest_with_problem(self) -> tuple[str, int, int, str]:
        contest_slug, contest_id, actor_user_id = self.create_contest("revision-build")
        _contest_problem_id, problem_id, problem_slug = self.add_owned_problem(
            contest_id,
            actor_user_id,
            "A",
            "revision-build-problem",
        )
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
            Path(config.settings.artifacts_root)
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
                archive.relative_to(config.settings.artifacts_root).as_posix(),
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                f"pv-{uuid.uuid4().hex}",
                now,
                now,
            ],
        )
        return materialization_id

    def test_build_preflight_requires_native_without_starting_export(self) -> None:
        contest_slug, contest_id, _problem_id, problem_slug = self._contest_with_problem()
        with patch.object(config.export_service, "create_export") as create_export:
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["icpc_bundle"],
            )

        self.assertEqual(response.status_code, 303)
        job_id = parse_qs(urlparse(str(response.headers["location"])).query)["job_id"][0]
        job = db_fetch_one("SELECT status FROM contest_jobs WHERE id=?", [job_id])
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "failed")
        summary = read_contest_job_summary(contest_id, job_id)
        self.assertIn(problem_slug, list(summary["missing_materializations"]))
        create_export.assert_not_called()

    def test_requested_outputs_share_one_frozen_revision_mapping(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        source_commit = "a" * 40
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit=source_commit,
            revision_number=12,
        )

        def submit(*, fn, **_kwargs):
            fn()
            return None, True, "queued"

        with (
            patch.object(config.worker_queue_service, "submit", side_effect=submit),
            patch(
                "app.impl.contest.shared._run_contest_pdf_job_worker",
                return_value={"artifact_id": "ca-pdf", "error": ""},
            ) as build_pdf,
            patch(
                "app.impl.contest.shared._run_contest_package_job_worker",
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
        self.assertEqual(int(items[0]["revision_number"]), 12)
        self.assertEqual(str(items[0]["materialization_id"]), materialization_id)
        self.assertTrue(str(items[0]["archive_sha256"]))
        build_pdf.assert_called_once()
        build_icpc.assert_called_once()
        self.assertEqual(build_pdf.call_args.kwargs["job_id"], job_id)
        self.assertEqual(build_icpc.call_args.kwargs["job_id"], job_id)
        summary = read_contest_job_summary(contest_id, job_id)
        self.assertEqual(summary["successful_outputs"], ["statement_pdf", "icpc_bundle"])

    def test_partial_output_does_not_change_the_frozen_revision(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="b" * 40,
            revision_number=7,
        )

        def submit(*, fn, **_kwargs):
            fn()
            return None, True, "queued"

        with (
            patch.object(config.worker_queue_service, "submit", side_effect=submit),
            patch(
                "app.impl.contest.shared._run_contest_pdf_job_worker",
                return_value={"artifact_id": "ca-pdf", "error": ""},
            ),
            patch(
                "app.impl.contest.shared._run_contest_package_job_worker",
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
