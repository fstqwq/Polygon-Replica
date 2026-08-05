from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.responses import FileResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_operation import audit
from app.impl.workspace.context_ui import page_ctx
from app.service.repository.merge import MergeFile, MergePreview


def _workspace_context(problem: str, user: str) -> tuple[dict, Path]:
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    return ctx, Path(ctx["workspace"]["path"])


def _file_view(row: MergeFile | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "path": row.path,
        "size": row.size,
        "executable": row.executable,
    }


def _preview_view(preview: MergePreview) -> dict[str, object]:
    entries_by_group: dict[str, list[dict[str, object]]] = {
        group_id: [] for group_id, _paths in preview.groups
    }
    for entry in preview.entries:
        entries_by_group[entry.group_id].append(
            {
                "entry_id": entry.entry_id,
                "path": entry.path,
                "current": _file_view(entry.current),
                "latest": _file_view(entry.latest),
            }
        )
    return {
        "id": preview.preview_id,
        "created_at": datetime.fromtimestamp(preview.created_at, timezone.utc).isoformat(),
        "suggested_available": preview.suggested_available,
        "groups": [
            {"id": group_id, "entries": entries_by_group[group_id]}
            for group_id, _paths in preview.groups
        ],
        "affected_count": len(preview.entries),
    }


def _render_merge(
    request: Request,
    problem: str,
    user: str,
    preview: MergePreview,
    *,
    step: int,
    choices: dict[str, str] | None = None,
    mode: str = "",
    message: str = "",
):
    ctx = page_ctx(problem, user, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    return template_response(
        request,
        "merge.html",
        {
            "ctx": ctx,
            "preview": _preview_view(preview),
            "step": step,
            "choices": choices or {},
            "review_mode": mode,
            "message": message,
        },
    )


def merge_start(problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx, workspace = _workspace_context(problem, user)
    try:
        preview = config.workspace_merge_service.start_preview(user, problem, workspace)
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "workspace.merge.preview",
            {"preview_id": preview.preview_id, "affected_files": len(preview.entries)},
        )
        return redirect_response(f"/problems/{problem}/merge/{preview.preview_id}")
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


def merge_page(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        step = 2 if request.query_params.get("mode") == "manual" else 1
        return _render_merge(request, problem, user, preview, step=step)
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


async def merge_review(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        form = await request.form()
        mode = str(form.get("mode") or "manual")
        if mode == "suggested":
            if not preview.suggested_available:
                raise ValueError("a suggested result is not available")
            return _render_merge(request, problem, user, preview, step=3, mode=mode)
        if mode != "manual":
            raise ValueError("select a merge result")
        choices = {
            group_id: str(form.get(f"choice_{group_id}") or "")
            for group_id, _paths in preview.groups
        }
        if any(side not in {"current", "latest"} for side in choices.values()):
            raise ValueError("choose a result for every affected file")
        return _render_merge(
            request, problem, user, preview, step=3, choices=choices, mode=mode
        )
    except Exception as exc:
        try:
            preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
            return _render_merge(request, problem, user, preview, step=2, message=str(exc))
        except Exception:
            return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


async def merge_apply(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx, _workspace = _workspace_context(problem, user)
    try:
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        form = await request.form()
        mode = str(form.get("mode") or "")
        choices = {
            group_id: str(form.get(f"choice_{group_id}") or "")
            for group_id, _paths in preview.groups
        }
        config.workspace_merge_service.apply_preview(user, problem, preview_id, mode, choices)
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "workspace.merge.apply",
            {"preview_id": preview_id, "mode": mode},
        )
        return redirect_response(
            f"/problems/{problem}/workspace",
            message="files updated; review the result before committing",
        )
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/merge/{preview_id}", message=str(exc))


def merge_cancel(
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        config.workspace_merge_service.cancel_preview(user, problem, preview_id)
        message = "merge preview cancelled"
    except Exception as exc:
        message = str(exc)
    return redirect_response(f"/problems/{problem}/workspace", message=message)


def merge_undo(problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx, workspace = _workspace_context(problem, user)
    try:
        config.workspace_merge_service.undo(workspace)
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "workspace.merge.undo",
            {},
        )
        message = "previous files restored"
    except Exception as exc:
        message = str(exc)
    return redirect_response(f"/problems/{problem}/workspace", message=message)


def merge_file(
    problem: str,
    preview_id: str,
    entry_id: str,
    side: str,
    user: Annotated[str, Depends(require_session_user)],
):
    _workspace_context(problem, user)
    path, _descriptor = config.workspace_merge_service.entry_file(
        user, problem, preview_id, entry_id, side
    )
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, content_disposition_type="inline")
