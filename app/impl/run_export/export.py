import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import workspace_access_context
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_job import start_export_job
from app.impl.workspace.context_ui import page_ctx


def _format_display(package_format: str) -> str:
    return {
        "domjudge": "DOMjudge",
        "icpc-2025-09": "ICPC 2025-09",
    }.get(package_format, package_format or "-")


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
    try:
        published = runtime().problem_package_service.published_revision(problem_id)
        published_revision_number: int | None = published.revision_number
        published_commit = published.source_commit
    except (OSError, RuntimeError, ValueError):
        published_revision_number = None
        published_commit = ""
    readiness = runtime().problem_package_service.published_readiness(problem_id)
    jobs = runtime().export_service.problem_export_jobs(problem_id, limit=40)
    actor_user_id = int(ctx["user"]["id"])
    access = ctx["access"]
    activity_rows: list[dict[str, object]] = []
    for row in jobs:
        if not runtime().access_query.package_job_context(
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            job_actor_user_id=row["actor_user_id"],
            status=row["status"],
            problem_access=access,
        )["can_view"]:
            continue
        revision_number = runtime().problem_package_service.revision_number(
            problem_id,
            row["source_commit"],
        )
        filename = Path(row["filename"]).name if row["filename"] else ""
        activity_rows.append(
            {
                "created_at": row["created_at"],
                "status": row["status"],
                "phase": runtime().export_service.job_phase(row),
                "format_display": _format_display(row["export_type"]),
                "source_display": (
                    f"v{revision_number}" if revision_number is not None else "unavailable"
                ),
                "detail": row["error"] or filename or row["status"],
                "export_id": row["export_id"] if filename else "",
                "filename": filename,
            }
        )
    verified_revisions = [
        {
            "id": row["id"],
            "revision_number": row["revision_number"],
            "source_commit": row["source_commit"],
            "verification_id": row["verification_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "reason": row["unavailable_reason"],
            "can_download": row["status"] == "available",
        }
        for row in runtime().problem_package_service.verified_revision_history(
            problem_id,
            limit=40,
        )
    ]
    return template_response(
        request,
        "export.html",
        {
            "ctx": ctx,
            "published_revision_number": published_revision_number,
            "published_commit": published_commit,
            "verified_readiness": readiness,
            "activity_rows": activity_rows,
            "verified_revisions": verified_revisions,
        },
    )


def export_create(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    format: str = Form(...),  # pylint: disable=redefined-builtin
):
    user_ctx = global_user_ctx(user)
    problem_row = runtime().contest_service.problem_by_slug(problem)
    if problem_row is None:
        return redirect_response(
            f"/problems/{problem}/export",
            status_code=303,
            message="problem not found",
        )
    problem_id = int(problem_row["id"])
    actor_user_id = int(user_ctx["user"]["id"])
    access = workspace_access_context(problem_id, actor_user_id)
    if not access["can_create_packages"]:
        return redirect_response(
            f"/problems/{problem}/export",
            status_code=303,
            message=access["package_create_block_reason"],
        )
    package_format = format.lower()
    job_id = f"exp-{uuid.uuid4().hex[:12]}"
    try:
        if package_format not in {"domjudge", "icpc-2025-09"}:
            raise ValueError("unsupported package format")
        started = start_export_job(
            runtime(),
            problem,
            user,
            actor_user_id=actor_user_id,
            problem_id=problem_id,
            requested_format=package_format,
            export_job_id=job_id,
        )
        if not started:
            raise HTTPException(
                status_code=409,
                detail="another package export is already running for this revision",
            )
        message = "package export queued"
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
    return redirect_response(
        f"/problems/{problem}/export",
        status_code=303,
        message=message,
    )
