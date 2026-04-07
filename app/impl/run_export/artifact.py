from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from app.impl.runtime.config import config
from app.impl.workspace.artifact import (
    assert_workspace_artifact_access,
    browser_file_response,
    export_download_filename,
    safe_artifact_path,
    verification_artifact_blob,
)
from app.impl.workspace.context_ui import page_ctx


def _browser_blob_response(blob: bytes, filename: str) -> Response:
    download_name = Path(filename).name or "artifact.bin"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{download_name}"',
    }
    suffix = Path(download_name).suffix.lower()
    if suffix == ".pdf":
        return Response(content=blob, media_type="application/pdf", headers=headers)
    if suffix in {".log", ".txt", ".tex", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".in", ".out", ".ans"}:
        return Response(content=blob, media_type="text/plain; charset=utf-8", headers=headers)
    return Response(content=blob, media_type="application/octet-stream", headers=headers)


def artifact_file(problem: str, user: str, verification_id: str, rel_path: str):
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
        virtual_blob = verification_artifact_blob(verification_id, rel_norm)
        if virtual_blob is not None:
            blob, filename = virtual_blob
            return _browser_blob_response(blob, filename)
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


def export_file(problem: str, user: str, export_id: str, filename: str):
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
