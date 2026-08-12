from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import HTTPException, Depends
from fastapi.responses import FileResponse

from app.impl.runtime.dependency import runtime
from app.impl.workspace.artifact import (
    assert_workspace_artifact_access,
    browser_file_response,
    safe_artifact_path,
    verification_artifact_file,
)
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.access import workspace_access_context


def _browser_blob_response(file_path: Path, filename: str) -> FileResponse:
    download_name = Path(filename).name or "artifact.bin"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{download_name}"',
    }
    suffix = Path(download_name).suffix.lower()
    if suffix == ".pdf":
        return FileResponse(file_path, filename=download_name, media_type="application/pdf", headers=headers)
    if suffix in {".log", ".txt", ".tex", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".in", ".out", ".ans"}:
        return FileResponse(file_path, filename=download_name, media_type="text/plain; charset=utf-8", headers=headers)
    return FileResponse(file_path, filename=download_name, media_type="application/octet-stream", headers=headers)


def artifact_file(problem: str, user: Annotated[str, Depends(require_session_user)], verification_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    assert_workspace_artifact_access(ctx, verification_id)
    rel_norm = rel_path.lstrip('/')
    if ".." in Path(rel_norm).parts:
        raise HTTPException(status_code=400, detail="invalid artifact path")
    virtual_file = verification_artifact_file(verification_id, rel_norm)
    if virtual_file is not None:
        payload_file, filename = virtual_file
        return _browser_blob_response(payload_file.path, filename)
    if not str(verification_id or "").startswith("p-"):
        raise HTTPException(status_code=404, detail="artifact file not found")
    try:
        file_path = safe_artifact_path(problem, verification_id, rel_norm)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="preview artifact expired")
        raise
    return browser_file_response(file_path)


def export_file(problem: str, user: Annotated[str, Depends(require_session_user)], export_id: str, filename: str):
    user_ctx = global_user_ctx(user)
    problem_row = runtime().contest_service.problem_by_slug(problem)
    if problem_row is None:
        raise HTTPException(status_code=404, detail="problem not found")
    problem_id = int(problem_row["id"])
    actor_user_id = int(user_ctx["user"]["id"])
    access = workspace_access_context(problem_id, actor_user_id)
    export = runtime().export_service.export_problem(export_id)
    export_access = runtime().access_query.package_export_context(
        actor_user_id=actor_user_id,
        expected_problem_id=problem_id,
        export=export,
        problem_access=access,
    )
    if not export_access["can_download"]:
        raise HTTPException(status_code=404, detail="artifact file not found")
    file_path = runtime().export_service.export_archive_path(
        problem_id,
        export_id,
        filename,
    )
    if file_path is None:
        raise HTTPException(status_code=404, detail="artifact file not found")
    download_name = Path(filename).name or "package.zip"
    return FileResponse(file_path, filename=download_name)


def materialization_file(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    materialization_id: str,
):
    user_ctx = global_user_ctx(user)
    problem_row = runtime().contest_service.problem_by_slug(problem)
    if problem_row is None:
        raise HTTPException(status_code=404, detail="problem not found")
    problem_id = int(problem_row["id"])
    materialization = runtime().problem_package_service.materialization(
        materialization_id
    )
    materialization_access = runtime().access_query.package_materialization_context(
        actor_user_id=int(user_ctx["user"]["id"]),
        expected_problem_id=problem_id,
        materialization=materialization,
    )
    if not materialization_access["can_download"]:
        raise HTTPException(status_code=404, detail="package not found")
    try:
        materialization, file_path = runtime().problem_package_service.native_archive(
            materialization_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="package unavailable") from exc
    slug = Path(str(problem_row["slug"])).name or "problem"
    filename = f"{slug}-native-v{materialization['revision_number']}.zip"
    return FileResponse(file_path, filename=filename)
