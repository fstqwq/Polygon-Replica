from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.impl.auth.public import template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import audit
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
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
    if problem_count <= 0:
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
    query = f"job_id={quote_plus(job_id)}" if job_id else ""
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=query, message=message)

def contest_packages_build_start(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "packages")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    problem_count_row = config.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [contest_id])
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
    if problem_count <= 0:
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
    query = f"job_id={quote_plus(job_id)}" if job_id else ""
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "packages", query=query, message=message)

def contest_packages_job_status(contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    job = _load_contest_job(contest_id, str(job_id or "").strip())
    if job is None:
        return JSONResponse({"ok": False, "running": False, "job_id": str(job_id or "").strip(), "status": "missing"}, status_code=404)
    status = str(job.get("status") or "").strip().lower()
    return JSONResponse(
        {
            "ok": True,
            "running": status == "running",
            "job_id": str(job.get("id") or ""),
            "job_type": str(job.get("job_type") or ""),
            "status": status,
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
        }
    )

def contest_packages_artifact_download(contest: str, user: str, artifact_id: str):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    safe_artifact_id = str(artifact_id or "").strip()
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
    file_path = Path(str(row["artifact_path"] or "")).resolve()
    base = _contest_artifacts_base()
    if base not in file_path.parents:
        raise HTTPException(status_code=404, detail="contest artifact not found")
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        raise HTTPException(status_code=404, detail="contest artifact file not found")
    download_name = Path(str(row["filename"] or "")).name.strip() or file_path.name
    return FileResponse(file_path, filename=download_name)

def contest_packages_page(request: Request, contest: str, user: str, job_id: str = ""):
    ctx = _contest_ctx(contest, user, "packages")
    contest_id = int(ctx["contest"]["id"])
    requested_job_id = str(job_id or "").strip()
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
        raw = str(item.get("summary_json") or "").strip()
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
        selected_job = _load_contest_job(contest_id, str(display_job_rows[0].get("id") or ""))
    base = _contest_artifacts_base()
    display_artifacts: list[dict[str, object]] = []
    for row in artifact_rows:
        item = dict(row)
        safe_id = str(item.get("id") or "").strip()
        safe_path = Path(str(item.get("artifact_path") or "")).resolve()
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
    problem_count = int(problem_count_row["c"] or 0) if problem_count_row is not None else 0
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


