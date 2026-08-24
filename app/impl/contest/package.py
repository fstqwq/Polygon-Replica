from typing import Annotated

from fastapi import Depends, Form, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.impl.auth.session import require_session_user
from app.impl.contest.shared import _contest_ctx
from app.impl.runtime.dependency import runtime


def contest_packages_download(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    package_format: str = Form(...),
):
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
    if not runtime().export_service.package_adapters.supports(package_format):
        raise HTTPException(status_code=400, detail="unsupported Contest package format")

    try:
        download = runtime().contest_package_service.build_download(
            contest_id=int(ctx["contest"]["id"]),
            contest_slug=str(ctx["contest"]["slug"]),
            package_format=package_format,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return FileResponse(
        download.path,
        filename=download.filename,
        media_type="application/zip",
        background=BackgroundTask(download.close),
    )
