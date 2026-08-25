import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.impl.auth.session import require_session_user
from app.impl.contest.shared import _contest_ctx
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_job import start_ready_external_export_job
from app.service.contest.package import ContestPackageSnapshot
from app.service.export.service import CachedExternalPackage
from app.service.platform.error_text import bounded_display_text
from app.service.platform.worker_queue import WorkerFuture


def _bounded_error_detail(error: object) -> str:
    return bounded_display_text(
        str(error) or type(error).__name__,
        limit_bytes=runtime().config_values.integer(
            "AUX_DISPLAY_TEXT_LIMIT_BYTES"
        ),
    )


def _require_download_access(
    contest: str,
    user: str,
) -> dict:
    ctx = _contest_ctx(contest, user, "overview")
    if not ctx["access"]["can_download_packages"]:
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["package_block_reason"],
        )
    roster = runtime().contest_service.contest_problems(
        int(ctx["contest"]["id"])
    )
    problem_access = runtime().access_query.problem_contexts(
        [int(row["problem_id"]) for row in roster],
        int(ctx["user"]["id"]),
    )
    blocked = [
        str(row["problem_slug"])
        for row in roster
        if not problem_access[int(row["problem_id"])]["can_read"]
    ]
    if blocked:
        raise HTTPException(
            status_code=403,
            detail="problem read access required: " + ", ".join(blocked),
        )
    return ctx


def _cached_external_packages(
    snapshot: ContestPackageSnapshot,
) -> dict[int, CachedExternalPackage]:
    result: dict[int, CachedExternalPackage] = {}
    for item in snapshot.items:
        cached = runtime().export_service.cached_external_package(
            problem_id=item.problem_id,
            native_package_id=item.native_package_id,
            package_format=snapshot.package_format,
        )
        if cached is not None:
            result[item.problem_id] = cached
    return result


def _recheck_download_access(contest: str, user: str) -> None:
    try:
        _require_download_access(contest, user)
    except HTTPException as exc:
        raise ValueError("Contest or access changed; retry download") from exc


def _prepare_external_packages(
    snapshot: ContestPackageSnapshot,
    *,
    actor_user_id: int,
) -> dict[int, CachedExternalPackage]:
    cached = _cached_external_packages(snapshot)
    pending: dict[int, WorkerFuture] = {}
    admission_failures: list[str] = []
    admission_failed_ids: set[int] = set()
    for item in snapshot.items:
        if item.problem_id in cached:
            continue
        try:
            _job_id, future = start_ready_external_export_job(
                runtime(),
                item.problem_slug,
                actor_user_id=actor_user_id,
                problem_id=item.problem_id,
                requested_format=snapshot.package_format,
                source_commit=item.source_commit,
                native_package_id=item.native_package_id,
                native_archive_sha256=item.archive_sha256,
                export_job_id=f"exp-{uuid.uuid4().hex[:12]}",
            )
            pending[item.problem_id] = future
        except (OSError, RuntimeError, ValueError) as exc:
            admission_failed_ids.add(item.problem_id)
            admission_failures.append(
                f"{item.idx} {item.problem_slug} [{snapshot.package_format}]: "
                f"{str(exc) or type(exc).__name__}"
            )
    for pending_future in set(pending.values()):
        pending_future.join()

    cached = _cached_external_packages(snapshot)
    failed = list(admission_failures)
    for item in snapshot.items:
        if item.problem_id in admission_failed_ids:
            continue
        item_future = pending.get(item.problem_id)
        error = item_future.exception() if item_future is not None else None
        if error is None and item.problem_id in cached:
            continue
        detail = str(error or "external package did not become ready")
        failed.append(
            f"{item.idx} {item.problem_slug} [{snapshot.package_format}]: {detail}"
        )
    if failed:
        raise ValueError(_bounded_error_detail("; ".join(failed)))
    return cached


def _contest_statement_pdfs(
    snapshot: ContestPackageSnapshot,
    *,
    actor_user_id: int,
    username: str,
) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for language in snapshot.statement_languages:
        preview = runtime().contest_statement_preview_service.build_pdf(
            snapshot.contest_id,
            contest_slug=snapshot.contest_slug,
            user_id=actor_user_id,
            username=username,
            source_kind="native_package",
            language=language,
            native_package_ids={
                item.problem_id: item.native_package_id
                for item in snapshot.items
            },
        )
        if preview["status"] != "ok":
            detail = str(
                preview["summary"].get("error")
                or "Contest statement PDF generation failed"
            )
            raise ValueError(
                f"failed to build {language} Contest statement: {detail}"
            )
        path = runtime().statement_preview_service.pdf(
            preview["id"],
            actor_user_id=actor_user_id,
        )
        if path is None:
            raise ValueError(
                f"failed to build {language} Contest statement: PDF is unavailable"
            )
        result[language] = path
    return result


def contest_packages_download(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    package_format: str = Form(...),
):
    ctx = _require_download_access(contest, user)
    if not runtime().export_service.package_adapters.supports(package_format):
        raise HTTPException(status_code=400, detail="unsupported Contest package format")

    try:
        snapshot = runtime().contest_package_service.freeze_download(
            contest_id=int(ctx["contest"]["id"]),
            contest_slug=str(ctx["contest"]["slug"]),
            package_format=package_format,
        )
        external_packages = _prepare_external_packages(
            snapshot,
            actor_user_id=int(ctx["user"]["id"]),
        )
        _recheck_download_access(contest, user)
        runtime().contest_package_service.validate_snapshot(snapshot)
        statement_pdfs = _contest_statement_pdfs(
            snapshot,
            actor_user_id=int(ctx["user"]["id"]),
            username=user,
        )
        _recheck_download_access(contest, user)
        runtime().contest_package_service.validate_snapshot(snapshot)
        external_packages = _cached_external_packages(snapshot)
        if len(external_packages) != len(snapshot.items):
            raise ValueError("Contest external package cache changed; retry download")
        download = runtime().contest_package_service.build_download(
            snapshot,
            external_packages=external_packages,
            statement_pdfs=statement_pdfs,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=409,
            detail="Contest or access changed; retry download",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=409,
            detail=_bounded_error_detail(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=_bounded_error_detail(exc),
        ) from exc

    return FileResponse(
        download.path,
        filename=download.filename,
        media_type="application/zip",
        background=BackgroundTask(download.close),
    )
