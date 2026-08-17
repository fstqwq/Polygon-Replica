import uuid
from typing import Annotated

from fastapi import Request, Depends, HTTPException

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.workspace_scope import add_contest_problem_hrefs
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_job import start_export_job
from app.impl.workspace.context_model import (
    package_published_revision_pair,
    workspace_revision_notice,
)
from app.service.export.service import NATIVE_PACKAGE_FORMAT

from app.impl.contest.shared import _contest_ctx, _contest_redirect


def contest_overview_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "overview", request=request)
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    source_rows = add_contest_problem_hrefs(
        request,
        contest_slug=str(ctx["contest"]["slug"]),
        rows=runtime().contest_problem_query_service.problem_rows(
            contest_id,
            str(ctx["user"]["username"]),
            user_id,
            include_review=True,
        ),
    )
    rows: list[dict[str, object]] = []
    for source_row in source_rows:
        row = dict(source_row)
        readiness = source_row["readiness"]
        row["revision_pair"] = (
            package_published_revision_pair(readiness)
            if readiness is not None
            else None
        )
        row["workspace_revision_notice"] = (
            workspace_revision_notice(readiness)
            if readiness is not None
            else None
        )
        if readiness is not None:
            verification = readiness["verification"]
            row["verification_display"] = verification["display"]
            row["verification_tone"] = (
                "warn"
                if verification["tone"] == "warning"
                else "danger" if verification["tone"] == "danger" else ""
            )
        else:
            row["verification_display"] = "unavailable"
            row["verification_tone"] = "danger"
        rows.append(row)
    owner_prefix_chars = max(
        (len(str(row["slug_owner"])) + 1 for row in rows),
        default=0,
    )
    package_states = [
        row["readiness"]["package"]["state"]
        for row in source_rows
        if row["readiness"] is not None
    ]
    package_ready_count = package_states.count("ready")
    package_stale_count = package_states.count("stale")
    package_queued_count = package_states.count("queued")
    package_none_count = (
        len(rows)
        - package_ready_count
        - package_queued_count
        - package_stale_count
    )
    package_all_ready = package_ready_count == len(rows)
    return template_response(
        request,
        "contest_overview.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "owner_prefix_chars": owner_prefix_chars,
            "package_ready_count": package_ready_count,
            "package_queued_count": package_queued_count,
            "package_stale_count": package_stale_count,
            "package_none_count": package_none_count,
            "package_all_ready": package_all_ready,
        },
    )


def contest_build_all_packages(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "overview")
    access = ctx["access"]
    if not access["can_build"]:
        raise HTTPException(status_code=403, detail=access["build_block_reason"])

    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    actor_username = str(ctx["user"]["username"])
    rows = runtime().contest_service.contest_problems(contest_id)
    problem_ids = [row["problem_id"] for row in rows]
    problem_access = runtime().access_query.problem_contexts(
        problem_ids,
        actor_user_id,
    )
    buildable = [
        row
        for row in rows
        if problem_access[row["problem_id"]]["can_create_packages"]
    ]
    readiness = runtime().problem_package_service.published_readiness_many(
        [row["problem_id"] for row in buildable]
    )

    queued = 0
    current = 0
    unavailable = len(rows) - len(buildable)
    for row in buildable:
        state = readiness[row["problem_id"]]["status"]
        if state == "ready":
            current += 1
            continue
        if state == "queued":
            queued += 1
            continue
        try:
            start_export_job(
                runtime(),
                row["problem_slug"],
                actor_username,
                actor_user_id=actor_user_id,
                problem_id=row["problem_id"],
                requested_format=NATIVE_PACKAGE_FORMAT,
                export_job_id=f"exp-{uuid.uuid4().hex[:12]}",
            )
            queued += 1
        except (OSError, RuntimeError, ValueError):
            unavailable += 1

    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "overview",
        message=(
            f"Build all packages: {queued} queued, "
            f"{current} current, {unavailable} unavailable"
        ),
    )
