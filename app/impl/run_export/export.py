import uuid
from typing import Annotated, Literal, TypedDict

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
    NativePackage,
)
from app.service.problem.readiness import PackageReadiness


class AvailablePackage(TypedDict):
    name: str
    download_href: str


class PackageFormatContext(TypedDict):
    id: str
    name: str


class CurrentPackageContext(TypedDict):
    published_display: str
    package_state: Literal["ready", "queued", "none"]
    package_verified: bool
    create_href: str
    external_formats: list[PackageFormatContext]


class StatementPreviewLink(TypedDict):
    language: str
    output_kind: Literal["html", "pdf"]
    label: str
    href: str


_STATEMENT_PREVIEW_OUTPUTS: tuple[
    tuple[Literal["html", "pdf"], str],
    ...,
] = (
    ("html", "problem_statement_html_page"),
    ("pdf", "problem_statement_pdf_page"),
)


class PackageRevisionRow(TypedDict):
    revision_number: int
    current: bool
    verified: bool
    package_download_href: str
    statement_preview_links: list[StatementPreviewLink]
    external_packages: list[AvailablePackage]


class PackageAttemptRow(TypedDict):
    created_at: str
    status: str
    format_display: str
    detail: str


def _available_packages_by_revision(
    request: Request,
    problem: str,
    problem_id: int,
    native_package_ids: list[str],
) -> dict[str, dict[str, AvailablePackage]]:
    href = problem_href_builder(request, problem)
    packages: dict[str, dict[str, AvailablePackage]] = {
        native_package_id: {} for native_package_id in native_package_ids
    }
    for row in runtime().export_service.materialization_packages(
        problem_id,
        native_package_ids,
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


def _current_native_package(
    native_package_id: str | None,
) -> NativePackage | None:
    if not native_package_id:
        return None
    native_package = runtime().problem_package_service.native_package(
        native_package_id
    )
    if native_package is None or native_package["status"] != "available":
        return None
    return native_package


def _revision_rows(
    request: Request,
    problem: str,
    published_commit: str,
    native_packages: list[NativePackage],
    verified_by_id: dict[str, bool],
    packages_by_revision: dict[str, dict[str, AvailablePackage]],
) -> list[PackageRevisionRow]:
    href = problem_href_builder(request, problem)
    rows: list[PackageRevisionRow] = []
    for native_package in native_packages:
        external_packages: list[AvailablePackage] = []
        statement_preview_links: list[StatementPreviewLink] = []
        stored_packages = packages_by_revision[native_package["id"]]
        for adapter in runtime().export_service.package_adapters.adapters:
            package = stored_packages.get(adapter.format)
            if package is not None:
                external_packages.append(package)
        for language in runtime().problem_package_service.statement_languages(
            native_package["id"]
        ):
            language_display = language.title()
            for output_kind, route_name in _STATEMENT_PREVIEW_OUTPUTS:
                statement_preview_links.append(
                    {
                        "language": language,
                        "output_kind": output_kind,
                        "label": (
                            f"Statements ({output_kind.upper()}, "
                            f"{language_display})"
                        ),
                        "href": href(
                            route_name,
                            query={
                                "source": "native_package",
                                "native_package_id": native_package["id"],
                                "language": language,
                            },
                        ),
                    }
                )
        rows.append(
            {
                "revision_number": native_package["revision_number"],
                "current": native_package["source_commit"] == published_commit,
                "verified": verified_by_id.get(native_package["id"], False),
                "package_download_href": href(
                    "native_package_file",
                    native_package_id=native_package["id"],
                ),
                "statement_preview_links": statement_preview_links,
                "external_packages": external_packages,
            }
        )
    return rows


def _current_package_context(
    request: Request,
    problem: str,
    readiness: PackageReadiness,
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
    package_state: Literal["ready", "queued", "none"] = "none"
    if readiness["state"] == "ready":
        package_state = "ready"
    elif readiness["state"] == "queued":
        package_state = "queued"
    revision_number = readiness["published_revision_number"]
    return {
        "published_display": (
            f"v{revision_number}" if revision_number is not None else "none"
        ),
        "package_state": package_state,
        "package_verified": bool(readiness["verified"]),
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
                "format_display": (
                    "Package"
                    if job["export_type"] == NATIVE_PACKAGE_FORMAT
                    else runtime()
                    .export_service.package_format_display_name(job["export_type"])
                ),
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
    current_native_package = _current_native_package(
        package_readiness["native_package_id"]
    )
    native_packages = (
        runtime().problem_package_service.available_native_package_history(
            problem_id,
            limit=40,
        )
    )
    native_package_ids = [row["id"] for row in native_packages]
    if (
        current_native_package is not None
        and current_native_package["id"] not in native_package_ids
    ):
        native_package_ids.append(current_native_package["id"])
    verified_by_id = (
        runtime().problem_package_service.native_packages_verified_many(
            native_packages
        )
    )
    packages_by_revision = _available_packages_by_revision(
        request,
        problem,
        problem_id,
        native_package_ids,
    )
    actor_user_id = int(ctx["user"]["id"])
    access: ProblemAccessContext = ctx["access"]
    current_package = _current_package_context(
        request,
        problem,
        package_readiness,
    )
    revision_rows = _revision_rows(
        request,
        problem,
        package_readiness["published_commit"],
        native_packages,
        verified_by_id,
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
    standard_solution_only: bool,
) -> str:
    readiness = runtime().problem_package_service.published_readiness(problem_id)
    native_package = _current_native_package(
        (
            readiness["native_package_id"]
            if readiness["status"] == "ready"
            else None
        )
    )
    if native_package is None:
        return ""
    href = problem_href_builder(request, problem)
    if package_format == NATIVE_PACKAGE_FORMAT:
        if not readiness["verified"] and not standard_solution_only:
            return ""
        return href(
            "native_package_file",
            native_package_id=native_package["id"],
        )
    packages = runtime().export_service.materialization_packages(
        problem_id,
        [native_package["id"]],
    )
    for package in packages:
        if package["export_type"] == package_format:
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
    standard_solution_only: bool,
) -> None:
    started = start_export_job(
        runtime(),
        problem,
        user,
        actor_user_id=actor_user_id,
        problem_id=problem_id,
        requested_format=package_format,
        export_job_id=f"exp-{uuid.uuid4().hex[:12]}",
        standard_solution_only=standard_solution_only,
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
    standard_solution_only: str | None = Form(None),
    create_native: str | None = Form(None),
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
        requested_format = (
            NATIVE_PACKAGE_FORMAT if create_native == "1" else format.lower()
        )
        package_format = runtime().export_service.require_job_format(
            requested_format
        )
        run_standard_solution_only = standard_solution_only == "1"
        existing_href = _existing_current_package_href(
            request,
            problem,
            problem_id,
            package_format,
            run_standard_solution_only,
        )
        if existing_href:
            return redirect_response(existing_href, status_code=303)
        _start_current_package(
            problem,
            user,
            actor_user_id,
            problem_id,
            package_format,
            run_standard_solution_only,
        )
        message = "package export queued"
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
    return redirect_response(
        page_href,
        status_code=303,
        message=message,
    )
