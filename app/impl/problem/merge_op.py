from __future__ import annotations

import mimetypes
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import json_error_response, redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_operation import audit
from app.impl.workspace.context_ui import page_ctx
from app.service.repository.merge import MergeEntry, MergeFile, MergePreview


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
        "content_kind": row.content_kind,
    }


def _change_kind(left: MergeFile | None, right: MergeFile | None) -> str:
    if left is None:
        return "added"
    if right is None:
        return "deleted"
    if left.sha256 != right.sha256:
        return "modified"
    if left.executable != right.executable:
        return "mode"
    return "unchanged"


def _entry_view(
    entry: MergeEntry,
    target: str,
    *,
    change_kind: str = "",
) -> dict[str, object]:
    right = entry.suggested if target == "suggested" else entry.latest
    kind = change_kind or _change_kind(entry.current, right)
    descriptors = [row for row in (entry.current, right) if row is not None]
    content_kind = "text"
    if any(row.content_kind == "binary" for row in descriptors):
        content_kind = "binary"
    elif any(row.content_kind == "large" for row in descriptors):
        content_kind = "large"
    return {
        "entry_id": entry.entry_id,
        "group_id": entry.group_id,
        "path": entry.path,
        "change_kind": kind,
        "change_label": {
            "added": "Added",
            "deleted": "Deleted",
            "modified": "Modified",
            "mode": "Permissions",
            "type-conflict": "Type conflict",
            "unchanged": "Unchanged",
        }[kind],
        "content_kind": content_kind,
        "current": _file_view(entry.current),
        "latest": _file_view(entry.latest),
        "suggested": _file_view(entry.suggested),
    }


def _preview_view(preview: MergePreview) -> dict[str, object]:
    entries_by_group: dict[str, list[dict[str, object]]] = {
        group_id: [] for group_id, _paths in preview.groups
    }
    group_sizes = {group_id: len(paths) for group_id, paths in preview.groups}
    for entry in preview.entries:
        kind = "type-conflict" if group_sizes[entry.group_id] > 1 else ""
        entries_by_group[entry.group_id].append(
            _entry_view(entry, "latest", change_kind=kind)
        )
    return {
        "id": preview.preview_id,
        "created_at": datetime.fromtimestamp(preview.created_at, timezone.utc).isoformat(),
        "suggested_available": preview.suggested_available,
        "suggested_entries": [
            _entry_view(entry, "suggested") for entry in preview.suggested_entries
        ],
        "groups": [
            {"id": group_id, "entries": entries_by_group[group_id]}
            for group_id, _paths in preview.groups
        ],
        "manual_affected_count": len(preview.entries),
        "suggested_affected_count": len(preview.suggested_entries),
    }


def _render_merge(
    request: Request,
    problem: str,
    user: str,
    preview: MergePreview,
    *,
    screen: str,
    choices: dict[str, str] | None = None,
    mode: str = "",
    selected_entry_id: str = "",
    message: str = "",
):
    ctx = page_ctx(problem, user, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    ctx["page_single_column"] = True
    ctx["page_wide_content"] = True
    ctx["merge_ui"] = True
    ctx["page_title"] = "Review Shared Changes"
    return template_response(
        request,
        "merge.html",
        {
            "ctx": ctx,
            "preview": _preview_view(preview),
            "screen": screen,
            "choices": choices or {},
            "review_mode": mode,
            "selected_entry_id": selected_entry_id,
            "message": message,
        },
    )


def _form_choices(preview: MergePreview, form) -> dict[str, str]:
    return {
        group_id: str(form.get(f"choice_{group_id}") or "")
        for group_id, _paths in preview.groups
    }


def merge_start(problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx, workspace = _workspace_context(problem, user)
    try:
        if config.workspace_merge_service.advance_clean_workspace(workspace):
            config.workspace_service.refresh_workspace_status_with_ids(
                workspace,
                int(ctx["problem"]["id"]),
                int(ctx["user"]["id"]),
            )
            audit(
                int(ctx["user"]["id"]),
                int(ctx["problem"]["id"]),
                "workspace.merge.auto_update",
                {},
            )
            return redirect_response(
                f"/problems/{problem}/workspace",
                message="Updated to the latest shared version.",
            )
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
        requested_mode = str(request.query_params.get("mode") or "")
        mode = "manual" if requested_mode == "manual" or not preview.suggested_available else "suggested"
        return _render_merge(request, problem, user, preview, screen="review", mode=mode)
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


async def merge_review(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    form = None
    try:
        form = await request.form()
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        mode = str(form.get("mode") or "manual")
        selected_entry_id = str(form.get("selected_entry_id") or "")
        if mode == "suggested":
            if not preview.suggested_available:
                raise ValueError("a suggested result is not available")
            return _render_merge(
                request,
                problem,
                user,
                preview,
                screen="confirm",
                mode=mode,
                selected_entry_id=selected_entry_id,
            )
        if mode != "manual":
            raise ValueError("select an update result")
        choices = _form_choices(preview, form)
        if any(side not in {"current", "latest"} for side in choices.values()):
            raise ValueError("choose a result for every affected file")
        return _render_merge(
            request,
            problem,
            user,
            preview,
            screen="confirm",
            choices=choices,
            mode=mode,
            selected_entry_id=selected_entry_id,
        )
    except Exception as exc:
        if form is None:
            return redirect_response(f"/problems/{problem}/merge/{preview_id}", message=str(exc))
        try:
            preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
            mode = str(form.get("mode") or "manual")
            return _render_merge(
                request,
                problem,
                user,
                preview,
                screen="review",
                choices=_form_choices(preview, form),
                mode=mode,
                selected_entry_id=str(form.get("selected_entry_id") or ""),
                message=str(exc),
            )
        except Exception:
            return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


async def merge_edit(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        form = await request.form()
        mode = str(form.get("mode") or "manual")
        if mode not in {"manual", "suggested"}:
            raise ValueError("select an update result")
        return _render_merge(
            request,
            problem,
            user,
            preview,
            screen="review",
            choices=_form_choices(preview, form),
            mode=mode,
            selected_entry_id=str(form.get("selected_entry_id") or ""),
        )
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/workspace", message=str(exc))


def merge_compare(
    request: Request,
    problem: str,
    preview_id: str,
    entry_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        _workspace_context(problem, user)
        target = str(request.query_params.get("target") or "latest")
        comparison = config.workspace_merge_service.comparison(
            user,
            problem,
            preview_id,
            entry_id,
            target,
        )
        return JSONResponse(asdict(comparison))
    except HTTPException:
        raise
    except Exception as exc:
        return json_error_response(str(exc), status_code=400)


async def merge_apply(
    request: Request,
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx, workspace = _workspace_context(problem, user)
    try:
        preview = config.workspace_merge_service.get_preview(user, problem, preview_id)
        form = await request.form()
        mode = str(form.get("mode") or "")
        choices = _form_choices(preview, form)
        affected_count = (
            len(preview.suggested_entries) if mode == "suggested" else len(preview.entries)
        )
        config.workspace_merge_service.apply_preview(user, problem, preview_id, mode, choices)
        audit(
            int(ctx["user"]["id"]),
            int(ctx["problem"]["id"]),
            "workspace.merge.apply",
            {"preview_id": preview_id, "mode": mode, "affected_files": affected_count},
        )
        changes = config.git_service.status_change_summary(workspace)
        changed_count = int(changes.get("total") or 0)
        if changed_count:
            message = (
                f"Updated to the latest shared version; review {changed_count} changed "
                f"file{'s' if changed_count != 1 else ''} before sharing"
            )
        else:
            message = "Updated to the latest shared version; your files now match it"
        return redirect_response(f"/problems/{problem}/workspace", message=message)
    except Exception as exc:
        return redirect_response(f"/problems/{problem}/merge/{preview_id}", message=str(exc))


def merge_cancel(
    problem: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    try:
        config.workspace_merge_service.cancel_preview(user, problem, preview_id)
        message = "update review cancelled"
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
        message = "files from before the update were restored"
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
    path, descriptor = config.workspace_merge_service.entry_file(
        user, problem, preview_id, entry_id, side
    )
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    disposition = "inline" if descriptor.content_kind == "text" else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type=media_type,
        content_disposition_type=disposition,
    )
