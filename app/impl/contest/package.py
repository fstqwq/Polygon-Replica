from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.service.platform.process import is_canonical_artifact_id

from .shared import (
    _CONTEST_JOB_TYPE_PACKAGE,
    _CONTEST_JOB_TYPE_PREVIEW,
    _contest_artifacts_base,
    _contest_ctx,
    _contest_redirect,
    _load_contest_job,
    _queue_contest_job,
)
def contest_packages_preview_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    if (int(problem_count_row["c"]) if problem_count_row is not None else 0) <= 0:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", message="add at least one problem first")
    job_id, queued, reason = _queue_contest_job(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        actor_user_id=int(ctx["user"]["id"]),
        actor_username=str(ctx["user"]["username"]),
        job_type=_CONTEST_JOB_TYPE_PREVIEW,
    )
    if queued:
        message = "contest preview queued"
    elif reason == "already_running":
        message = f"contest preview already running ({job_id})"
    else:
        message = f"contest preview queue rejected ({reason})"
    audit(
        int(ctx["user"]["id"]),
        None,
        "contest.packages.preview.start",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "queued": bool(queued),
            "reason": reason,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=f"job_id={quote_plus(job_id)}" if job_id else "", message=message)

def contest_packages_build_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    if (int(problem_count_row["c"]) if problem_count_row is not None else 0) <= 0:
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
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=f"job_id={quote_plus(job_id)}" if job_id else "", message=message)

def contest_packages_job_status(contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    job = _load_contest_job(contest_id, job_id.strip())
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
    safe_artifact_id = artifact_id.strip()
    if not is_canonical_artifact_id(safe_artifact_id):
        raise HTTPException(status_code=404, detail="contest artifact not found")
    row = config.db.fetch_one(
        """
        SELECT id,filename,artifact_path
        FROM contest_artifacts
        WHERE contest_id=? AND id=?
        """,
        [contest_id, safe_artifact_id],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    file_path = Path(row["artifact_path"]).resolve()
    base = _contest_artifacts_base()
    if base not in file_path.parents:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="contest artifact file not found")
    return FileResponse(file_path, filename=Path(row["filename"]).name or file_path.name)

def contest_packages_page(request: Request, contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    requested_job_id = str(job_id).strip()
    artifact_rows = config.db.fetch_all(
        """
        SELECT id,job_id,artifact_type,filename,artifact_path,size_bytes,created_at
        FROM contest_artifacts
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        [contest_id],
    )
    job_rows = config.db.fetch_all(
        """
        SELECT id,job_type,status,summary_json,created_at,finished_at
        FROM contest_jobs
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        [contest_id],
    )
    display_job_rows: list[dict[str, object]] = []
    for row in job_rows:
        item = dict(row)
        summary: dict[str, object] = {}
        raw = item.get("summary_json")
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = {}
            if isinstance(parsed, dict):
                summary = parsed
        item["summary"] = summary
        display_job_rows.append(item)
    selected_job = _load_contest_job(contest_id, requested_job_id)
    if selected_job is None and display_job_rows:
        selected_job = _load_contest_job(contest_id, display_job_rows[0]["id"])
    base = _contest_artifacts_base()
    display_artifacts: list[dict[str, object]] = []
    for row in artifact_rows:
        item = dict(row)
        safe_id = item["id"]
        safe_path = Path(item["artifact_path"]).resolve()
        downloadable = bool(
            safe_id
            and is_canonical_artifact_id(safe_id)
            and base in safe_path.parents
            and safe_path.exists()
            and safe_path.is_file()
            and (not safe_path.is_symlink())
        )
        item["downloadable"] = downloadable
        item["download_href"] = (
            f"/contests/{ctx['contest']['slug']}/{ctx['user']['username']}/packages/artifacts/{safe_id}"
            if downloadable
            else ""
        )
        display_artifacts.append(item)
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"]) if problem_count_row is not None else 0
    return template_response(
        request,
        "contest_packages.html",
        {
            "ctx": ctx,
            "artifact_rows": display_artifacts,
            "job_rows": display_job_rows,
            "selected_job": selected_job,
            "problem_count": problem_count,
            "requested_job_id": requested_job_id,
        },
    )


