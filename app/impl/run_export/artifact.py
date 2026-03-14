from __future__ import annotations

from fastapi.responses import Response

from app.impl.run_export.context import (
    Path,
    HTTPException,
    FileResponse,
    assert_workspace_artifact_access,
    browser_file_response,
    contains_symlink_component,
    export_download_filename,
    normalize_run_id_token,
    redirect_response,
    safe_artifact_path,
    safe_run_artifact_path,
    workspace_run_artifact_root,
    config,
    is_canonical_artifact_id,
    page_ctx,
    quote_plus,
    workspace_verification_id_for_run,
)


def _browser_blob_response(blob: bytes, filename: str) -> Response:
    safe_name = Path(str(filename or "")).name or "artifact.bin"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="{safe_name}"',
    }
    suffix = Path(safe_name).suffix.lower()
    if suffix == ".pdf":
        return Response(content=blob, media_type="application/pdf", headers=headers)
    text_like_suffixes = {".log", ".txt", ".tex", ".json", ".md", ".csv", ".xml", ".yaml", ".yml", ".in", ".out", ".ans"}
    if suffix in text_like_suffixes:
        return Response(content=blob, media_type="text/plain; charset=utf-8", headers=headers)
    return Response(content=blob, media_type="application/octet-stream", headers=headers)
def _verification_artifact_root(problem_id: int, verification_id: str) -> Path | None:
    safe_verification_id = str(verification_id or "").strip()
    if (not safe_verification_id) or (not is_canonical_artifact_id(safe_verification_id)):
        return None
    row = config.db.fetch_one(
        "SELECT artifact_path FROM verifications WHERE id=? AND problem_id=?",
        [safe_verification_id, int(problem_id)],
    )
    if row is None:
        return None
    artifact_path = str(row["artifact_path"] or "").strip()
    if not artifact_path:
        return None
    try:
        root = Path(artifact_path).resolve()
        base = config.settings.artifacts_root.resolve()
    except Exception:
        return None
    try:
        if (root != base and base not in root.parents) or (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            return None
    except OSError:
        return None
    return root

def _is_safe_regular_file(path: Path) -> bool:
    try:
        return path.exists() and path.is_file() and (not path.is_symlink())
    except OSError:
        return False

def artifact_file(problem: str, user: str, verification_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    assert_workspace_artifact_access(ctx, verification_id)
    rel_norm = str(rel_path or '').lstrip('/')
    if rel_norm.startswith("export/"):
        from app.impl.run_export.export import _resolve_export_archive_path

        export_name = Path(rel_norm).name
        file_path = _resolve_export_archive_path(problem, verification_id, export_name)
        if file_path is None:
            raise HTTPException(status_code=404, detail="artifact file not found")
    else:
        file_path = safe_artifact_path(problem, verification_id, rel_path)
    if rel_norm.startswith('export/'):
        export_name = Path(rel_norm).name
        download_name = export_download_filename(ctx, verification_id, export_name)
        if download_name:
            return FileResponse(file_path, filename=download_name)
    return browser_file_response(file_path)

def run_artifact_file(problem: str, user: str, run_id: str, rel_path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    rel_norm = str(rel_path or '').strip().lstrip('/')
    if Path(rel_norm).name == 'compile.log':
        raise HTTPException(status_code=403, detail='compile.log download is disabled')
    if rel_norm.startswith("cache://"):
        workspace_run_artifact_root(ctx, run_id)
        service = getattr(config, "judgehost_task_service", None)
        if service is None:
            raise HTTPException(status_code=404, detail="run artifact file not found")
        blob = service.resolve_artifact_blob(rel_norm)
        if blob is None:
            raise HTTPException(status_code=404, detail="run artifact file not found")
        return _browser_blob_response(blob, Path(rel_norm).name)
    try:
        file_path = safe_run_artifact_path(ctx, run_id, rel_path)
    except HTTPException as exc:
        detail = str(getattr(exc, "detail", "") or "").strip().lower()
        if int(getattr(exc, "status_code", 500)) == 404 and detail.startswith("run artifact"):
            try:
                run_root = workspace_run_artifact_root(ctx, run_id)
                candidate = run_root / str(rel_path or "").strip().lstrip("/")
                if contains_symlink_component(run_root, candidate):
                    raise
            except HTTPException:
                raise
            except Exception:
                pass
            safe_run_id = normalize_run_id_token(run_id)
            verification_id = workspace_verification_id_for_run(ctx, safe_run_id)
            target = f"/problems/{problem}/{user}/run/details?verification_id={quote_plus(verification_id or str(run_id or '').strip())}"
            return redirect_response(
                target,
                status_code=303,
                message="Run artifact is not persisted; rerun verification to regenerate downloadable files.",
            )
        raise
    return browser_file_response(file_path)


