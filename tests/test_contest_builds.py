import hashlib
import json
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException

from app.service.contest.package import ContestPackageService
from app.service.export.adapters import PackageAdapterRegistry
from tests.contest_support import ContestActionBase
from tests.db_helpers import (
    db_connection,
    db_execute,
    db_fetch_all,
    db_fetch_one,
    read_contest_job_summary,
)
from tests.ui_support import contest_packages_build_start, runtime, uuid


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
        status: str = "available",
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
        archive.write_bytes(f"verified:{source_commit}".encode("ascii"))
        payload = archive.read_bytes()
        now = "2026-08-08T00:00:00+00:00"
        db_execute(
            """
            INSERT INTO problem_package_materializations(
                id,problem_id,source_commit,revision_number,source_digest,
                archive_rel_path,archive_sha256,archive_size_bytes,
                verification_id,status,created_at,checked_at,unavailable_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                f"ver-{uuid.uuid4().hex[:16]}",
                status,
                now,
                now,
                "" if status == "available" else "fixture unavailable",
            ],
        )
        return materialization_id

    def _freeze(self, contest_id: int, contest_slug: str) -> dict[str, object]:
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        return runtime.contest_service.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=int(actor["id"]),
            job_type="build",
            summary={"job_type": "build", "contest_slug": contest_slug},
        )

    def test_missing_verified_revision_returns_409_without_creating_job(self) -> None:
        contest_slug, contest_id, _problem_id, _problem_slug = self._contest_with_problem()

        with self.assertRaises(HTTPException) as raised:
            contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["domjudge_bundle"],
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("not_ready:", str(raised.exception.detail))
        self.assertEqual(
            db_fetch_all("SELECT id FROM contest_jobs WHERE contest_id=?", [contest_id]),
            [],
        )

    def test_freeze_selects_latest_available_revision_and_ignores_newer_unavailable(
        self,
    ) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        expected_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="b" * 40,
            revision_number=8,
        )
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="c" * 40,
            revision_number=9,
            status="unavailable",
        )

        frozen = self._freeze(contest_id, contest_slug)

        self.assertEqual(frozen["outcome"], "created")
        item = db_fetch_one(
            """SELECT source_commit,revision_number,materialization_id,archive_sha256
               FROM contest_build_items WHERE job_id=?""",
            [frozen["job_id"]],
        )
        self.assertIsNotNone(item)
        self.assertEqual(str(item["source_commit"]), "b" * 40)
        self.assertEqual(int(item["revision_number"]), 8)
        self.assertEqual(str(item["materialization_id"]), expected_id)
        self.assertTrue(str(item["archive_sha256"]))

    def test_freeze_orders_by_idx_and_keeps_frozen_ordinals(self) -> None:
        contest_slug, contest_id, actor_user_id = self.create_contest("idx-freeze")
        contest_problem_ids: dict[str, int] = {}
        for idx in ("B", "A", "C"):
            contest_problem_id, problem_id, _problem_slug = self.add_owned_problem(
                contest_id,
                actor_user_id,
                idx,
                f"idx-freeze-{idx.lower()}",
            )
            contest_problem_ids[idx] = contest_problem_id
            self._seed_materialization(
                problem_id=problem_id,
                source_commit=idx.lower() * 40,
                revision_number=1,
            )

        frozen = self._freeze(contest_id, contest_slug)

        self.assertEqual(frozen["outcome"], "created")
        self.assertEqual(
            [
                (int(row["ordinal"]), str(row["idx"]))
                for row in db_fetch_all(
                    """SELECT ordinal,idx FROM contest_build_items
                       WHERE job_id=? ORDER BY ordinal""",
                    [frozen["job_id"]],
                )
            ],
            [(1, "A"), (2, "B"), (3, "C")],
        )

        runtime.contest_service.set_problem_indices(
            contest_id,
            [
                (contest_problem_ids["B"], "A"),
                (contest_problem_ids["A"], "B"),
                (contest_problem_ids["C"], "C"),
            ],
        )

        self.assertEqual(
            [
                (int(row["ordinal"]), str(row["idx"]))
                for row in db_fetch_all(
                    """SELECT ordinal,idx FROM contest_build_items
                       WHERE job_id=? ORDER BY ordinal""",
                    [frozen["job_id"]],
                )
            ],
            [(1, "A"), (2, "B"), (3, "C")],
        )

    def test_freeze_atomically_reuses_active_job(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="d" * 40,
            revision_number=3,
        )

        first = self._freeze(contest_id, contest_slug)
        second = self._freeze(contest_id, contest_slug)

        self.assertEqual(first["outcome"], "created")
        self.assertEqual(second["outcome"], "already_running")
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(
            len(
                db_fetch_all(
                    "SELECT id FROM contest_jobs WHERE contest_id=? AND job_type='build'",
                    [contest_id],
                )
            ),
            1,
        )

    def test_freeze_fails_immediately_when_sqlite_admission_is_busy(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="e" * 40,
            revision_number=3,
        )

        with db_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            frozen = self._freeze(contest_id, contest_slug)

        self.assertEqual(frozen["outcome"], "busy")
        self.assertEqual(frozen["job_id"], "")

    def test_all_outputs_share_one_reader_and_one_frozen_mapping(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="f" * 40,
            revision_number=7,
        )
        reader = SimpleNamespace(
            root=Path(runtime.settings.artifacts_root),
            manifest={"source_commit": "f" * 40},
        )
        opened: list[tuple[str, str]] = []

        @contextmanager
        def open_reader(
            requested_id: str,
            *,
            expected_archive_sha256: str,
        ) -> Iterator[object]:
            opened.append((requested_id, expected_archive_sha256))
            yield reader

        def submit(
            *, fn: Callable[[], None], **_kwargs: object
        ) -> tuple[None, bool, str]:
            fn()
            return None, True, "queued"

        package_formats: list[str] = []

        def staged_output(
            *,
            contest_slug: str,
            job_id: str,
            token: str,
            artifact_type: str,
            filename: str,
        ) -> dict[str, object]:
            path = (
                runtime.contest_service.job_root(contest_slug, job_id)
                / f"{token}.bin"
            )
            path.write_bytes(token.encode("ascii"))
            return {
                "artifact_id": "",
                "error": "",
                "_artifact_path": str(path),
                "_artifact_type": artifact_type,
                "_artifact_filename": filename,
            }

        def build_pdf(
            *,
            contest_slug: str,
            job_id: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            return staged_output(
                contest_slug=contest_slug,
                job_id=job_id,
                token="pdf",
                artifact_type="contest-pdf",
                filename="revision-build-english-statements.pdf",
            )

        def build_bundle(
            *,
            package_format: str,
            readers: dict[str, object],
            contest_slug: str,
            job_id: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            self.assertIs(readers[materialization_id], reader)
            package_formats.append(package_format)
            token = package_format.replace("-", "_")
            return staged_output(
                contest_slug=contest_slug,
                job_id=job_id,
                token=token,
                artifact_type=f"{package_format}-bundle",
                filename=f"revision-build-{package_format}.zip",
            )

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch.object(runtime.problem_package_service, "open_reader", side_effect=open_reader),
            patch.object(runtime.contest_snapshot_service, "create") as snapshot,
            patch.object(
                runtime.contest_statement_service,
                "build_pdf",
                side_effect=build_pdf,
            ) as build_pdf,
            patch.object(
                runtime.contest_package_service,
                "build_bundle",
                side_effect=build_bundle,
            ),
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=[
                    "statement_pdf",
                    "domjudge_bundle",
                    "icpc_2025_09_bundle",
                ],
                language="english",
            )

        self.assertEqual(response.status_code, 303)
        job_id = parse_qs(urlparse(str(response.headers["location"])).query)["job_id"][0]
        self.assertEqual(opened[0][0], materialization_id)
        self.assertEqual(len(opened), 1)
        snapshot.assert_called_once()
        build_pdf.assert_called_once()
        self.assertIs(build_pdf.call_args.kwargs["readers"][materialization_id], reader)
        self.assertEqual(package_formats, ["domjudge", "icpc-2025-09"])
        summary = read_contest_job_summary(contest_id, job_id)
        self.assertEqual(
            summary["successful_outputs"],
            ["statement_pdf", "domjudge_bundle", "icpc_2025_09_bundle"],
        )

    def test_package_only_build_does_not_snapshot_contest_sources(self) -> None:
        contest_slug, _contest_id, problem_id, _problem_slug = self._contest_with_problem()
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="1" * 40,
            revision_number=4,
        )

        @contextmanager
        def open_reader(*_args: object, **_kwargs: object) -> Iterator[object]:
            yield SimpleNamespace(root=Path("."), manifest={})

        def submit(
            *, fn: Callable[[], None], **_kwargs: object
        ) -> tuple[None, bool, str]:
            fn()
            return None, True, "queued"

        def build_bundle(
            *,
            contest_slug: str,
            job_id: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            path = runtime.contest_service.job_root(contest_slug, job_id) / "domjudge.zip"
            path.write_bytes(b"domjudge bundle")
            return {
                "artifact_id": "",
                "error": "",
                "_artifact_path": str(path),
                "_artifact_type": "domjudge-bundle",
                "_artifact_filename": "revision-build-domjudge.zip",
            }

        with (
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            patch.object(runtime.problem_package_service, "open_reader", side_effect=open_reader),
            patch.object(runtime.contest_snapshot_service, "create") as snapshot,
            patch.object(
                runtime.contest_package_service,
                "build_bundle",
                side_effect=build_bundle,
            ) as build_bundle,
        ):
            response = contest_packages_build_start(
                contest=contest_slug,
                user="alice",
                outputs=["domjudge_bundle"],
            )

        self.assertEqual(response.status_code, 303)
        snapshot.assert_not_called()
        self.assertIn(materialization_id, build_bundle.call_args.kwargs["readers"])

    def test_reader_exit_failure_discards_staged_output_without_publishing(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        materialization_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="5" * 40,
            revision_number=5,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        staged_paths: list[Path] = []

        @contextmanager
        def open_reader(
            requested_id: str,
            *,
            expected_archive_sha256: str,
        ) -> Iterator[object]:
            self.assertEqual(requested_id, materialization_id)
            self.assertTrue(expected_archive_sha256)
            yield SimpleNamespace(root=Path("."), manifest={})
            raise RuntimeError("reader checksum changed on exit")

        def build_bundle(
            *,
            contest_slug: str,
            job_id: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            staged = (
                runtime.contest_service.job_root(contest_slug, job_id)
                / "staged.zip"
            )
            staged.write_bytes(b"staged contest output")
            staged_paths.append(staged)
            return {
                "artifact_id": "",
                "error": "",
                "_artifact_path": str(staged),
                "_artifact_type": "domjudge-bundle",
                "_artifact_filename": "contest-domjudge.zip",
            }

        def submit(
            *, fn: Callable[[], None], **_kwargs: object
        ) -> tuple[None, bool, str]:
            fn()
            return None, True, "queued"

        with (
            patch.object(
                runtime.problem_package_service,
                "open_reader",
                side_effect=open_reader,
            ),
            patch.object(
                runtime.contest_package_service,
                "build_bundle",
                side_effect=build_bundle,
            ),
            patch.object(runtime.worker_queue_service, "submit", side_effect=submit),
            self.assertRaisesRegex(RuntimeError, "reader checksum changed on exit"),
        ):
            runtime.contest_build_service.queue(
                contest_id=contest_id,
                contest_slug=contest_slug,
                actor_user_id=int(actor["id"]),
                outputs=("domjudge_bundle",),
            )

        self.assertEqual(len(staged_paths), 1)
        self.assertFalse(staged_paths[0].exists())
        self.assertEqual(
            db_fetch_all(
                "SELECT id FROM contest_artifacts WHERE contest_id=?",
                [contest_id],
            ),
            [],
        )
        self.assertEqual(
            runtime.contest_service.list_artifacts(contest_id, limit=10),
            [],
        )

    def test_queue_submit_exception_terminalizes_frozen_job(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="6" * 40,
            revision_number=6,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)

        with (
            patch.object(
                runtime.worker_queue_service,
                "submit",
                side_effect=RuntimeError("queue submission exploded"),
            ),
            self.assertRaisesRegex(RuntimeError, "queue submission exploded"),
        ):
            runtime.contest_build_service.queue(
                contest_id=contest_id,
                contest_slug=contest_slug,
                actor_user_id=int(actor["id"]),
                outputs=("domjudge_bundle",),
            )

        job = db_fetch_one(
            """SELECT id,status,finished_at FROM contest_jobs
               WHERE contest_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            [contest_id],
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "failed")
        self.assertTrue(str(job["finished_at"] or ""))

    def test_summary_write_failure_terminalizes_frozen_job(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        self._seed_materialization(
            problem_id=problem_id,
            source_commit="7" * 40,
            revision_number=7,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)

        with (
            patch.object(
                runtime.contest_service,
                "_write_job_summary",
                side_effect=OSError("summary write failed"),
            ),
            self.assertRaisesRegex(OSError, "summary write failed"),
        ):
            runtime.contest_service.freeze_build_job(
                contest_id=contest_id,
                actor_user_id=int(actor["id"]),
                job_type="build",
                summary={"job_type": "build", "contest_slug": contest_slug},
            )

        job = db_fetch_one(
            """SELECT id,status,finished_at FROM contest_jobs
               WHERE contest_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            [contest_id],
        )
        self.assertIsNotNone(job)
        self.assertEqual(str(job["status"]), "failed")
        self.assertTrue(str(job["finished_at"] or ""))

    def test_bundle_is_not_published_when_any_child_package_fails(self) -> None:
        contest_slug, contest_id, problem_id, _problem_slug = self._contest_with_problem()
        first_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="2" * 40,
            revision_number=5,
        )
        actor = db_fetch_one("SELECT id FROM users WHERE username='alice'")
        self.assertIsNotNone(actor)
        _cp_id, second_problem_id, _slug = self.add_owned_problem(
            contest_id,
            int(actor["id"]),
            "B",
            "second-package-problem",
        )
        second_id = self._seed_materialization(
            problem_id=second_problem_id,
            source_commit="3" * 40,
            revision_number=2,
        )
        frozen = self._freeze(contest_id, contest_slug)

        class Adapter:
            format = "domjudge"
            accepts_short_name = True

            def build(
                self,
                _reader: object,
                *,
                short_name: str | None,
                target: Path,
                **_kwargs: object,
            ) -> str:
                if short_name == "B":
                    raise RuntimeError("package build failed")
                (target / "problem.yaml").write_text("name: A\n", encoding="utf-8")
                return ""

        class Registry:
            @staticmethod
            def require(package_format: str) -> Adapter:
                if package_format != "domjudge":
                    raise ValueError("unsupported package format")
                return Adapter()

        service = ContestPackageService(
            runtime.contest_service,
            cast(PackageAdapterRegistry, Registry()),
        )
        readers = {
            first_id: SimpleNamespace(root=Path("."), manifest={}),
            second_id: SimpleNamespace(root=Path("."), manifest={}),
        }

        result = service.build_bundle(
            contest_id=contest_id,
            contest_slug=contest_slug,
            job_id=str(frozen["job_id"]),
            package_format="domjudge",
            readers=readers,
        )

        self.assertEqual(result["totals"]["success"], 1)
        self.assertEqual(result["totals"]["failed"], 1)
        self.assertEqual(result["artifact_id"], "")
        self.assertEqual(
            db_fetch_all(
                "SELECT id FROM contest_artifacts WHERE job_id=?",
                [frozen["job_id"]],
            ),
            [],
        )

    def test_bundle_projects_directly_and_stages_frozen_identity(self) -> None:
        contest_slug, contest_id, problem_id, problem_slug = self._contest_with_problem()
        verified_revision_id = self._seed_materialization(
            problem_id=problem_id,
            source_commit="4" * 40,
            revision_number=6,
        )
        frozen = self._freeze(contest_id, contest_slug)
        calls: list[tuple[str, str | None]] = []

        class Adapter:
            format = "domjudge"
            accepts_short_name = True

            def build(
                self,
                _reader: object,
                *,
                short_name: str | None,
                target: Path,
                **_kwargs: object,
            ) -> str:
                calls.append((self.format, short_name))
                (target / "problem.yaml").write_text("name: Example\n", encoding="utf-8")
                return "package warning"

        class Registry:
            @staticmethod
            def require(package_format: str) -> Adapter:
                if package_format != "domjudge":
                    raise ValueError("unsupported package format")
                return Adapter()

        service = ContestPackageService(
            runtime.contest_service,
            cast(PackageAdapterRegistry, Registry()),
        )
        result = service.build_bundle(
            contest_id=contest_id,
            contest_slug=contest_slug,
            job_id=str(frozen["job_id"]),
            package_format="domjudge",
            readers={
                verified_revision_id: SimpleNamespace(root=Path("."), manifest={})
            },
        )

        self.assertEqual(calls, [("domjudge", "A")])
        self.assertEqual(
            result["warnings"],
            [{"problem": problem_slug, "message": "package warning"}],
        )
        self.assertEqual(result["artifact_id"], "")
        self.assertEqual(result["_artifact_type"], "domjudge-bundle")
        self.assertEqual(
            result["_artifact_filename"],
            f"{contest_slug}-domjudge-packages-{frozen['job_id']}.zip",
        )
        self.assertEqual(
            db_fetch_all(
                "SELECT id FROM contest_artifacts WHERE job_id=?",
                [frozen["job_id"]],
            ),
            [],
        )
        archive_path = Path(str(result["_artifact_path"]))
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "domjudge")
            self.assertEqual(manifest["problems"][0]["idx"], "A")
            self.assertEqual(manifest["problems"][0]["problem"], problem_slug)
            self.assertEqual(
                manifest["problems"][0]["verified_revision_id"],
                verified_revision_id,
            )
            self.assertTrue(manifest["problems"][0]["archive_sha256"])
            child_name = manifest["problems"][0]["package"]
            self.assertIn(child_name, archive.namelist())
        archive_path.unlink()
