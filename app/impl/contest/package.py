from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit

from .shared import (
    _CONTEST_JOB_TYPE_PACKAGE,
    _CONTEST_JOB_TYPE_PDF,
    _contest_ctx,
    _contest_redirect,
    _queue_contest_job,
)


def contest_packages_preview_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    if config.contest_service.problem_count(contest_id) <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", message="add at least one problem first")
    job_id, queued, reason = _queue_contest_job(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        actor_user_id=int(ctx["user"]["id"]),
        actor_username=str(ctx["user"]["username"]),
        job_type=_CONTEST_JOB_TYPE_PDF,
    )
    if queued:
        message = "contest pdf build queued"
    elif reason == "already_running":
        message = f"contest pdf build already running ({job_id})"
    else:
        message = f"contest pdf build queue rejected ({reason})"
    audit(
        int(ctx["user"]["id"]),
        None,
        "contest.packages.pdf.start",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "queued": bool(queued),
            "reason": reason,
        },
    )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        user,
        "packages",
        query=f"job_id={quote_plus(job_id)}" if job_id else "",
        message=message,
    )


def contest_packages_build_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    if config.contest_service.problem_count(contest_id) <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", message="add at least one problem first")
    job_id, queued, reason = _queue_contest_job(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        actor_user_id=int(ctx["user"]["id"]),
        actor_username=str(ctx["user"]["username"]),
        job_type=_CONTEST_JOB_TYPE_PACKAGE,
    )
    if queued:
        message = "contest package build queued"
    elif reason == "already_running":
        message = f"contest package build already running ({job_id})"
    else:
        message = f"contest package build queue rejected ({reason})"
    audit(
        int(ctx["user"]["id"]),
        None,
        "contest.packages.build.start",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "queued": bool(queued),
            "reason": reason,
        },
    )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        user,
        "packages",
        query=f"job_id={quote_plus(job_id)}" if job_id else "",
        message=message,
    )


def contest_packages_job_status(contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    job = config.contest_service.load_job(contest_id, job_id.strip())
    if job is None:
        return JSONResponse({"ok": False, "running": False, "job_id": job_id.strip(), "status": "missing"}, status_code=404)
    status = job["status"]
    return JSONResponse(
        {
            "ok": True,
            "running": status == "running",
            "job_id": job.get("id"),
            "job_type": job.get("job_type"),
            "status": status,
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
        }
    )


def contest_packages_artifact_download(contest: str, user: str, artifact_id: str):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    artifact = config.contest_service.artifact_download(contest_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    file_path, filename = artifact
    return FileResponse(file_path, filename=filename)


def contest_packages_page(request: Request, contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    requested_job_id = str(job_id).strip()
    artifact_rows = config.contest_service.list_artifacts(contest_id, limit=50)
    job_rows = config.contest_service.list_jobs(contest_id, limit=20)
    selected_job = config.contest_service.load_job(contest_id, requested_job_id)
    if selected_job is None and job_rows:
        selected_job = config.contest_service.load_job(contest_id, str(job_rows[0]["id"]))
    display_artifacts: list[dict[str, object]] = []
    for row in artifact_rows:
        item = dict(row)
        safe_id = str(item["id"] or "")
        item["download_href"] = (
            f"/contests/{ctx['contest']['slug']}/{ctx['user']['username']}/packages/artifacts/{safe_id}"
            if bool(item["downloadable"])
            else ""
        )
        display_artifacts.append(item)
    return template_response(
        request,
        "contest_packages.html",
        {
            "ctx": ctx,
            "artifact_rows": display_artifacts,
            "job_rows": job_rows,
            "selected_job": selected_job,
            "problem_count": config.contest_service.problem_count(contest_id),
            "requested_job_id": requested_job_id,
        },
    )
