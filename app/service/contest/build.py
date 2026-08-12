from __future__ import annotations

from typing import Protocol

from app.service.access.query import AccessQuery
from app.service.contest.model import ContestBuildRevision
from app.service.contest.package import ContestPackageService
from app.service.contest.service import ContestService
from app.service.contest.snapshot import ContestSourceSnapshotService
from app.service.contest.statement import ContestStatementService
from app.service.platform.worker_queue import WorkerQueueService
from app.service.problem_package.service import (
    MaterializationRow,
    ProblemPackageService,
    PublishedRevision,
)


class MaterializePublishedRevision(Protocol):
    def __call__(
        self,
        *,
        revision: PublishedRevision,
        actor_user_id: int,
        actor_username: str,
    ) -> MaterializationRow: ...


class ContestBuildService:
    """Own freeze, queue, execution, and terminal Contest build transitions."""

    def __init__(
        self,
        *,
        contest_service: ContestService,
        access_query: AccessQuery,
        package_service: ProblemPackageService,
        statement_service: ContestStatementService,
        contest_package_service: ContestPackageService,
        snapshot_service: ContestSourceSnapshotService,
        worker_queue: WorkerQueueService,
        materialize_revision: MaterializePublishedRevision,
    ) -> None:
        self._contest = contest_service
        self._access = access_query
        self._problem_packages = package_service
        self._statements = statement_service
        self._contest_packages = contest_package_service
        self._snapshots = snapshot_service
        self._workers = worker_queue
        self._materialize_revision = materialize_revision

    def queue(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        actor_user_id: int,
        actor_username: str,
        outputs: tuple[str, ...],
        language: str = "",
        insert_blank_pages: bool = False,
    ) -> tuple[str, bool, str]:
        access = self._access.contest_context(contest_id, actor_user_id)
        if not access["can_build"]:
            raise PermissionError(access["build_block_reason"])
        unknown_outputs = set(outputs).difference(
            {"statement_pdf", "icpc_bundle"}
        )
        if unknown_outputs:
            unknown = sorted(unknown_outputs)[0]
            raise ValueError(f"unsupported contest build output: {unknown}")
        requested_outputs = tuple(
            output
            for output in ("statement_pdf", "icpc_bundle")
            if output in set(outputs)
        )
        if not requested_outputs:
            raise ValueError("select at least one contest build output")
        job_language = (
            self._statements.resolve_language(contest_id, language)
            if "statement_pdf" in requested_outputs
            else ""
        )
        initial_summary: dict[str, object] = {
            "job_type": "build",
            "contest_slug": contest_slug,
            "requested_outputs": list(requested_outputs),
            "outputs": {},
        }
        if job_language:
            initial_summary["language"] = job_language
        revisions: list[ContestBuildRevision] = []
        for row in self._contest.contest_problems(contest_id):
            try:
                published = self._problem_packages.published_revision(
                    int(row["problem_id"])
                )
            except (OSError, RuntimeError, ValueError) as exc:
                return "", False, f"not_ready:{row['problem_slug']}: {exc}"
            revisions.append(
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "position": int(row["position"]),
                    "label": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "statement_folder": str(row["statement_folder"]),
                    "problem_slug": str(row["problem_slug"]),
                    "source_commit": published.source_commit,
                    "revision_number": published.revision_number,
                }
            )
        if not revisions:
            return "", False, "not_ready:contest has no problems"
        frozen = self._contest.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            job_type="build",
            summary=initial_summary,
            revisions=revisions,
        )
        job_id = str(frozen["job_id"])
        outcome = frozen["outcome"]
        if outcome == "already_running":
            return job_id, False, outcome
        if outcome in {"busy", "roster_changed"}:
            return "", False, outcome
        if outcome == "not_ready":
            return "", False, "not_ready:" + ",".join(frozen["blocked_problems"])

        def runner() -> None:
            self._run(
                contest_id=contest_id,
                contest_slug=contest_slug,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
                job_id=job_id,
                requested_outputs=requested_outputs,
                language=job_language,
                insert_blank_pages=insert_blank_pages,
                initial_summary=initial_summary,
            )

        _future, queued, reason = self._workers.submit(
            name=f"contest-build-{contest_id}",
            fn=runner,
            queue_name="contest-build",
            dedupe_key=f"contest:{contest_id}:build",
            job_type="contest-build",
        )
        if not queued:
            self._contest.update_job(
                contest_id,
                job_id,
                "failed",
                {
                    "job_type": "build",
                    "contest_slug": contest_slug,
                    "error": f"queue rejected ({reason})",
                },
                finished=True,
            )
        return job_id, queued, str(reason or "").strip()

    def _run(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        actor_user_id: int,
        actor_username: str,
        job_id: str,
        requested_outputs: tuple[str, ...],
        language: str,
        insert_blank_pages: bool,
        initial_summary: dict[str, object],
    ) -> None:
        try:
            self._contest.update_job(
                contest_id,
                job_id,
                "running",
                initial_summary,
                finished=False,
            )
            items = self._contest.build_items(job_id)
            source_folder_map = {
                int(entry["problem_id"]): str(entry["statement_folder"])
                for entry in items
                if entry["statement_folder"]
            }
            default_tex = (
                self._statements.default_statements_tex(
                    contest_id=contest_id,
                    contest_slug=contest_slug,
                    language=language,
                    problem_entries=items,
                    source_folder_map=source_folder_map,
                )
                if language
                else ""
            )
            self._snapshots.create(
                contest_slug=contest_slug,
                job_id=job_id,
                language=language,
                default_statements_tex=default_tex,
            )
            materializations = self._materialize_items(
                job_id=job_id,
                actor_user_id=actor_user_id,
                actor_username=actor_username,
            )
            failed = [row for row in materializations if row["status"] != "success"]
            if failed:
                self._contest.update_job(
                    contest_id,
                    job_id,
                    "failed",
                    {
                        **initial_summary,
                        "phase": "materialization",
                        "materializations": materializations,
                        "error": "; ".join(
                            f"{row['problem_slug']}: {row['error']}" for row in failed
                        ),
                    },
                    finished=True,
                )
                return
            output_results: dict[str, dict[str, object]] = {}
            for output in requested_outputs:
                try:
                    if output == "statement_pdf":
                        output_results[output] = self._statements.build_pdf(
                            contest_id=contest_id,
                            contest_slug=contest_slug,
                            job_id=job_id,
                            language=language,
                            insert_blank_pages=insert_blank_pages,
                        )
                    else:
                        output_results[output] = self._contest_packages.build_bundle(
                            contest_id=contest_id,
                            contest_slug=contest_slug,
                            job_id=job_id,
                        )
                except Exception as exc:
                    output_results[output] = {
                        "error": str(exc),
                        "artifact_id": "",
                    }
            successful = [
                output
                for output, summary in output_results.items()
                if summary.get("artifact_id") and not summary.get("error")
            ]
            if len(successful) == len(requested_outputs):
                status = "ok"
            elif successful:
                status = "partial"
            else:
                status = "failed"
            final_summary: dict[str, object] = {
                **initial_summary,
                "materializations": materializations,
                "outputs": output_results,
                "successful_outputs": successful,
            }
            if status != "ok":
                final_summary["error"] = "; ".join(
                    f"{output}: {summary.get('error') or 'build failed'}"
                    for output, summary in output_results.items()
                    if output not in successful
                )
            self._contest.update_job(
                contest_id,
                job_id,
                status,
                final_summary,
                finished=True,
            )
        except Exception as exc:
            self._contest.update_job(
                contest_id,
                job_id,
                "failed",
                {"job_type": "build", "error": str(exc) or "worker failed"},
                finished=True,
            )
            raise

    def _materialize_items(
        self,
        *,
        job_id: str,
        actor_user_id: int,
        actor_username: str,
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for entry in self._contest.build_items(job_id):
            result: dict[str, object] = {
                "problem_id": int(entry["problem_id"]),
                "problem_slug": str(entry["problem_slug"]),
                "source_commit": str(entry["source_commit"]),
                "revision_number": int(entry["revision_number"]),
                "status": "failed",
                "materialization_id": "",
                "error": "",
            }
            try:
                revision = self._problem_packages.published_revision_at(
                    int(entry["problem_id"]),
                    str(entry["source_commit"]),
                    int(entry["revision_number"]),
                )
                materialization = self._materialize_revision(
                    revision=revision,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
                self._contest.bind_build_item_materialization(
                    job_id=job_id,
                    contest_problem_id=int(entry["contest_problem_id"]),
                    problem_id=int(entry["problem_id"]),
                    source_commit=str(entry["source_commit"]),
                    materialization_id=materialization["id"],
                    archive_sha256=materialization["archive_sha256"],
                )
                result["status"] = "success"
                result["materialization_id"] = materialization["id"]
            except Exception as exc:
                result["error"] = str(exc)
            results.append(result)
        return results
