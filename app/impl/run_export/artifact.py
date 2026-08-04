from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import HTTPException, Depends
from fastapi.responses import FileResponse

from app.impl.runtime.config import config
from app.impl.workspace.artifact import (
    assert_workspace_artifact_access,
    browser_file_response,
    export_download_filename,
    safe_artifact_path,
    verification_artifact_file,
)
from app.impl.workspace.context_ui import page_ctx


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
    if rel_norm.startswith("export/"):
        from app.impl.run_export.export import _resolve_export_archive_path

        export_name = Path(rel_norm).name
        file_path = _resolve_export_archive_path(problem, verification_id, export_name)
        if file_path is None:
            raise HTTPException(status_code=404, detail="artifact file not found")
    else:
        virtual_file = verification_artifact_file(verification_id, rel_norm)
        if virtual_file is not None:
            payload_file, filename = virtual_file
            return _browser_blob_response(payload_file.path, filename)
        try:
            file_path = safe_artifact_path(problem, verification_id, rel_norm)
        except HTTPException as exc:
            if str(verification_id or "").startswith("p-") and exc.status_code == 404:
                raise HTTPException(status_code=404, detail="preview artifact expired")
            raise
    if rel_norm.startswith('export/'):
        export_name = Path(rel_norm).name
        download_name = export_download_filename(ctx, verification_id, export_name)
        if download_name:
            return FileResponse(file_path, filename=download_name)
    return browser_file_response(file_path)


def export_file(problem: str, user: Annotated[str, Depends(require_session_user)], export_id: str, filename: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    file_path = config.export_service.export_archive_path(
        problem_id,
        workspace_id,
        export_id,
        problem,
        filename,
    )
    if file_path is None:
        raise HTTPException(status_code=404, detail="artifact file not found")
    download_name = Path(filename).name or "package.zip"
    return FileResponse(file_path, filename=download_name)
