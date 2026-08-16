import uuid
from typing import Annotated, TypedDict

from fastapi import Depends, Form, HTTPException, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import (
    contest_workspace_context_from_request,
    problem_href_builder,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import workspace_access_context
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_job import start_export_job
from app.impl.workspace.context_ui import page_ctx
from app.service.access import ProblemAccessContext
from app.service.export.service import NATIVE_PACKAGE_FORMAT
from app.service.problem_package.service import (
    VerifiedRevision,
)


class AvailablePackage(TypedDict):
    name: str
    download_href: str


class PackageFormatContext(TypedDict):
    id: str
    name: str


class CurrentPackageContext(TypedDict):
    revision_number: int | None
    native_available: bool
    create_href: str
    external_formats: list[PackageFormatContext]


class PackageRevisionRow(TypedDict):
    revision_number: int
    current: bool
    native_download_href: str
    additional_packages: list[AvailablePackage]


class PackageAttemptRow(TypedDict):
    created_at: str
    status: str
    format_display: str
    detail: str


def _available_packages_by_revision(
    request: Request,
    problem: str,
    problem_id: int,
    verified_revision_ids: list[str],
) -> dict[str, dict[str, AvailablePackage]]:
    href = problem_href_builder(request, problem)
    packages: dict[str, dict[str, AvailablePackage]] = {
        verified_revision_id: {} for verified_revision_id in verified_revision_ids
    }
    for row in runtime().export_service.materialization_packages(
        problem_id,
        verified_revision_ids,
    ):
        package_format = row["export_type"]
        packages[row["materialization_id"]][package_format] = {
            "name": runtime().export_service.package_format_display_name(
                package_format
            ),
            "download_href": href(
                "export_file",
                export_id=row["export_id"],
                filename=row["filename"],
            ),
        }
    return packages


def _current_verified_revision(
    verified_revision_id: str | None,
) -> VerifiedRevision | None:
    if not verified_revision_id:
        return None
    verified_revision = runtime().problem_package_service.verified_revision(
        verified_revision_id
    )
    if verified_revision is None or verified_revision["status"] != "available":
        return None
    return verified_revision


def _revision_rows(
    request: Request,
    problem: str,
    published_commit: str,
    verified_revisions: list[VerifiedRevision],
    packages_by_revision: dict[str, dict[str, AvailablePackage]],
) -> list[PackageRevisionRow]:
    href = problem_href_builder(request, problem)
    rows: list[PackageRevisionRow] = []
    for verified_revision in verified_revisions:
        additional_packages: list[AvailablePackage] = []
        stored_packages = packages_by_revision[verified_revision["id"]]
        for adapter in runtime().export_service.package_adapters.adapters:
            package = stored_packages.get(adapter.format)
            if package is not None:
                additional_packages.append(package)
        rows.append(
            {
                "revision_number": verified_revision["revision_number"],
                "current": verified_revision["source_commit"] == published_commit,
                "native_download_href": href(
                    "verified_revision_file",
                    verified_revision_id=verified_revision["id"],
                ),
                "additional_packages": additional_packages,
            }
        )
    return rows


def _current_package_context(
    request: Request,
    problem: str,
    revision_number: int | None,
    verified_revision: VerifiedRevision | None,
) -> CurrentPackageContext:
    href = problem_href_builder(request, problem)
    external_formats: list[PackageFormatContext] = []
    for adapter in runtime().export_service.package_adapters.adapters:
        external_formats.append(
            {
                "id": adapter.format,
                "name": adapter.display_name,
            }
        )
    return {
        "revision_number": revision_number,
        "native_available": verified_revision is not None,
        "create_href": href("export_create"),
        "external_formats": external_formats,
    }


def _package_attempt_rows(
    problem_id: int,
    actor_user_id: int,
    access: ProblemAccessContext,
) -> list[PackageAttemptRow]:
    rows: list[PackageAttemptRow] = []
    for job in runtime().export_service.problem_export_jobs(problem_id, limit=40):
        if not runtime().access_query.package_job_context(
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            job_actor_user_id=job["actor_user_id"],
            status=job["status"],
            problem_access=access,
        )["can_view"]:
            continue
        detail = job["error"]
        if job["status"] == "succeeded":
            detail = f"Warning: {detail}" if detail else "Completed"
        elif not detail:
            detail = runtime().export_service.job_phase(job)
        rows.append(
            {
                "created_at": job["created_at"],
                "status": job["status"],
                "format_display": runtime()
                .export_service.package_format_display_name(job["export_type"]),
                "detail": detail,
            }
        )
    return rows


def export_page(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    problem_id = int(ctx["problem"]["id"])
    package_readiness = ctx["shell"]["readiness"]["package"]
    current_verified_revision = _current_verified_revision(
        package_readiness["verified_revision_id"]
    )
    verified_revisions = (
        runtime().problem_package_service.available_verified_revision_history(
            problem_id,
            limit=40,
        )
    )
    verified_revision_ids = [row["id"] for row in verified_revisions]
    if (
        current_verified_revision is not None
        and current_verified_revision["id"] not in verified_revision_ids
    ):
        verified_revision_ids.append(current_verified_revision["id"])
    packages_by_revision = _available_packages_by_revision(
        request,
        problem,
        problem_id,
        verified_revision_ids,
    )
    actor_user_id = int(ctx["user"]["id"])
    access: ProblemAccessContext = ctx["access"]
    current_package = _current_package_context(
        request,
        problem,
        package_readiness["published_revision_number"],
        current_verified_revision,
    )
    revision_rows = _revision_rows(
        request,
        problem,
        package_readiness["published_commit"],
        verified_revisions,
        packages_by_revision,
    )
    activity_rows = _package_attempt_rows(
        problem_id,
        actor_user_id,
        access,
    )

    return template_response(
        request,
        "export.html",
        {
            "ctx": ctx,
            "current_package": current_package,
            "activity_rows": activity_rows,
            "revision_rows": revision_rows,
        },
    )


def _existing_current_package_href(
    request: Request,
    problem: str,
    problem_id: int,
    package_format: str,
) -> str:
    readiness = runtime().problem_package_service.published_readiness(problem_id)
    verified_revision = _current_verified_revision(
        (
            readiness["verified_revision_id"]
            if readiness["status"] == "ready"
            else None
        )
    )
    if verified_revision is None:
        return ""
    href = problem_href_builder(request, problem)
    if package_format == NATIVE_PACKAGE_FORMAT:
        return href(
            "verified_revision_file",
            verified_revision_id=verified_revision["id"],
        )
    packages = runtime().export_service.materialization_packages(
        problem_id,
        [verified_revision["id"]],
    )
    for package in packages:
        if (
            package["export_type"] == package_format
            and runtime().export_service.export_archive_path(
                problem_id,
                package["export_id"],
                package["filename"],
            )
            is not None
        ):
            return href(
                "export_file",
                export_id=package["export_id"],
                filename=package["filename"],
            )
    return ""


def _start_current_package(
    problem: str,
    user: str,
    actor_user_id: int,
    problem_id: int,
    package_format: str,
) -> None:
    started = start_export_job(
        runtime(),
        problem,
        user,
        actor_user_id=actor_user_id,
        problem_id=problem_id,
        requested_format=package_format,
        export_job_id=f"exp-{uuid.uuid4().hex[:12]}",
    )
    if not started:
        raise HTTPException(
            status_code=409,
            detail="another package export is already running for this revision",
        )


def export_create(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    format: str = Form(...),  # pylint: disable=redefined-builtin
):
    user_ctx = global_user_ctx(user)
    problem_row = runtime().contest_service.problem_by_slug(problem)
    href = problem_href_builder(request, problem)
    page_href = href("problem_export")
    if problem_row is None:
        return redirect_response(
            page_href,
            status_code=303,
            message="problem not found",
        )
    problem_id = int(problem_row["id"])
    actor_user_id = int(user_ctx["user"]["id"])
    access = workspace_access_context(problem_id, actor_user_id)
    if not access["can_create_packages"]:
        return redirect_response(
            page_href,
            status_code=303,
            message=access["package_create_block_reason"],
        )
    try:
        package_format = runtime().export_service.require_job_format(format.lower())
        existing_href = _existing_current_package_href(
            request,
            problem,
            problem_id,
            package_format,
        )
        if existing_href:
            return redirect_response(existing_href, status_code=303)
        _start_current_package(
            problem,
            user,
            actor_user_id,
            problem_id,
            package_format,
        )
        message = "package export queued"
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
    return redirect_response(
        page_href,
        status_code=303,
        message=message,
    )
