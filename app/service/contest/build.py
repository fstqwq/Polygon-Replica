from contextlib import ExitStack
from pathlib import Path

from app.service.access.query import AccessQuery
from app.service.contest.package import ContestPackageService
from app.service.contest.service import ContestService
from app.service.platform.worker_queue import WorkerQueueService
from app.service.problem_package.service import (
    ProblemPackageService,
    NativePackageReader,
)


class ContestBuildService:
    """Own freeze, queue, execution, and terminal Contest build transitions."""

    def __init__(
        self,
        *,
        contest_service: ContestService,
        access_query: AccessQuery,
        package_service: ProblemPackageService,
        contest_package_service: ContestPackageService,
        worker_queue: WorkerQueueService,
    ) -> None:
        self._contest = contest_service
        self._access = access_query
        self._problem_packages = package_service
        self._contest_packages = contest_package_service
        self._workers = worker_queue

    @staticmethod
    def _staged_artifact_path(summary: dict[str, object]) -> Path | None:
        value = summary.get("_artifact_path")
        return Path(value) if isinstance(value, str) and value else None

    @classmethod
    def _discard_staged_outputs(
        cls,
        output_results: dict[str, dict[str, object]],
    ) -> None:
        for summary in output_results.values():
            path = cls._staged_artifact_path(summary)
            if path is not None:
                path.unlink(missing_ok=True)

    def _publish_output(
        self,
        *,
        contest_id: int,
        job_id: str,
        summary: dict[str, object],
    ) -> None:
        path = self._staged_artifact_path(summary)
        artifact_type = summary.get("_artifact_type")
        filename = summary.get("_artifact_filename")
        for key in ("_artifact_path", "_artifact_type", "_artifact_filename"):
            summary.pop(key, None)
        if path is None or not isinstance(artifact_type, str) or not artifact_type:
            summary["artifact_id"] = ""
            summary["error"] = "contest artifact staging metadata is incomplete"
            return
        if not isinstance(filename, str) or not filename:
            summary["error"] = "contest artifact filename is missing"
            path.unlink(missing_ok=True)
            return
        try:
            summary["filename"] = filename
            summary["artifact_id"] = self._contest.record_artifact(
                contest_id=contest_id,
                job_id=job_id,
                artifact_type=artifact_type,
                filename=filename,
                artifact_path=path,
            )
        except Exception as exc:
            summary["artifact_id"] = ""
            summary["error"] = str(exc) or "contest artifact publication failed"
        finally:
            path.unlink(missing_ok=True)

    def queue(
        self,
        *,
        contest_id: int,
        contest_slug: str,
        actor_user_id: int,
        outputs: tuple[str, ...],
    ) -> tuple[str, bool, str]:
        access = self._access.contest_context(contest_id, actor_user_id)
        if not access["can_build"]:
            raise PermissionError(access["build_block_reason"])
        supported_outputs = {
            "domjudge_bundle",
            "icpc_2025_09_bundle",
        }
        unknown_outputs = set(outputs).difference(supported_outputs)
        if unknown_outputs:
            unknown = sorted(unknown_outputs)[0]
            raise ValueError(f"unsupported contest build output: {unknown}")
        requested_outputs = tuple(
            output
            for output in (
                "domjudge_bundle",
                "icpc_2025_09_bundle",
            )
            if output in set(outputs)
        )
        if not requested_outputs:
            raise ValueError("select at least one contest build output")
        initial_summary: dict[str, object] = {
            "job_type": "build",
            "contest_slug": contest_slug,
            "requested_outputs": list(requested_outputs),
            "outputs": {},
        }
        frozen = self._contest.freeze_build_job(
            contest_id=contest_id,
            actor_user_id=actor_user_id,
            job_type="build",
            summary=initial_summary,
        )
        job_id = str(frozen["job_id"])
        outcome = frozen["outcome"]
        if outcome == "already_running":
            return job_id, False, outcome
        if outcome == "busy":
            return "", False, outcome
        if outcome == "not_ready":
            return "", False, "not_ready:" + ",".join(frozen["blocked_problems"])

        def runner() -> None:
            self._run(
                contest_id=contest_id,
                contest_slug=contest_slug,
                job_id=job_id,
                requested_outputs=requested_outputs,
                initial_summary=initial_summary,
            )

        try:
            _future, queued, reason = self._workers.submit(
                name=f"contest-build-{contest_id}",
                fn=runner,
                queue_name="contest-build",
                dedupe_key=f"contest:{contest_id}:build",
                job_type="contest-build",
            )
        except Exception as exc:
            self._contest.update_job(
                contest_id,
                job_id,
                "failed",
                {
                    "job_type": "build",
                    "contest_slug": contest_slug,
                    "error": str(exc) or "queue submission failed",
                },
                finished=True,
            )
            raise
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
        job_id: str,
        requested_outputs: tuple[str, ...],
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
            output_results: dict[str, dict[str, object]] = {}
            try:
                with ExitStack() as readers_stack:
                    readers: dict[str, NativePackageReader] = {}
                    for entry in items:
                        materialization_id = str(entry["materialization_id"])
                        readers[materialization_id] = readers_stack.enter_context(
                            self._problem_packages.open_reader(
                                materialization_id,
                                expected_archive_sha256=str(entry["archive_sha256"]),
                            )
                        )
                    for output in requested_outputs:
                        try:
                            package_format = (
                                "domjudge"
                                if output == "domjudge_bundle"
                                else "icpc-2025-09"
                            )
                            output_results[output] = self._contest_packages.build_bundle(
                                contest_id=contest_id,
                                contest_slug=contest_slug,
                                job_id=job_id,
                                package_format=package_format,
                                readers=readers,
                            )
                        except Exception as exc:
                            output_results[output] = {
                                "error": str(exc),
                                "artifact_id": "",
                            }
            except Exception:
                self._discard_staged_outputs(output_results)
                raise
            for summary in output_results.values():
                if not summary.get("error"):
                    self._publish_output(
                        contest_id=contest_id,
                        job_id=job_id,
                        summary=summary,
                    )
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
                "native_packages": [
                    {
                        "problem_slug": str(entry["problem_slug"]),
                        "revision_number": int(entry["revision_number"]),
                        "source_commit": str(entry["source_commit"]),
                        "native_package_id": str(entry["materialization_id"]),
                        "archive_sha256": str(entry["archive_sha256"]),
                    }
                    for entry in items
                ],
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
