from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile, Depends
from fastapi.responses import FileResponse

from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit, build_line_focus_context, build_repo_browser_entries, default_files_selected_path, files_browse_query_tail, kind_for_path, parse_line_param, template_for_kind
from app.impl.workspace.solution import ensure_solution_metadata_for_source
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import normalize_workspace_rel_path, safe_workspace_path

_C = config.constants


def _files_redirect_href(
    problem: str,
    user: str,
    *,
    path: str = '',
    browse_tail: str = '',
) -> str:
    query_parts: list[str] = []
    if path:
        query_parts.append(f'path={quote_plus(path)}')
    if browse_tail:
        query_parts.append(browse_tail.lstrip('&'))
    if not query_parts:
        return f'/problems/{problem}/files'
    return f'/problems/{problem}/files?' + '&'.join(query_parts)

def files_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    selected = normalize_workspace_rel_path(request.query_params.get('path'))
    line_raw = request.query_params.get('line')
    selected_line = parse_line_param(line_raw, default=1)
    content = ''
    content_truncated = False
    selected_missing = False
    selected_is_dir = False
    selected_is_binary = False
    selected_is_pdf = False
    selected_media_type = ''
    auto_message = ''
    files, files_truncated = config.workspace_file_service.list_paths(
        workspace,
        limit=_C.WORKSPACE_FILE_LIST_LIMIT,
        require_allowed_root=False,
    )
    default_selected = default_files_selected_path(workspace, files)
    if not selected:
        selected = default_selected
    try:
        selected = config.workspace_file_service.normalize_path(selected, require_allowed_root=False)
        selected_view = config.workspace_file_service.file_view(
            workspace,
            selected,
            char_limit=_C.WORKSPACE_FILE_VIEW_CHAR_LIMIT,
            require_allowed_root=False,
        )
    except (HTTPException, ValueError):
        selected = default_selected
        selected_view = config.workspace_file_service.file_view(
            workspace,
            selected,
            char_limit=_C.WORKSPACE_FILE_VIEW_CHAR_LIMIT,
            require_allowed_root=False,
        )
        auto_message = f'invalid path; opened {selected}'
    selected_missing = not selected_view.exists
    selected_is_dir = selected_view.is_dir
    selected_is_binary = selected_view.is_binary
    selected_is_pdf = selected_view.is_pdf
    selected_media_type = selected_view.media_type
    content = selected_view.content
    content_truncated = selected_view.content_truncated
    selected_template_kind = kind_for_path(selected)
    selected_parent = str(Path(selected).parent)
    if selected_parent in {'.', ''}:
        selected_parent = ''
    requested_dir = request.query_params.get('dir')
    browse_dir_default = ''
    browse_dir, browse_parent, browse_dirs, browse_files, browse_total = build_repo_browser_entries(workspace, files, requested_dir if requested_dir is not None else browse_dir_default)
    browse_query_tail = files_browse_query_tail(browse_dir)
    line_focus = build_line_focus_context(content, selected_line) if line_raw else None
    line_jump_requested = bool(line_raw)
    line_jump_missing = bool(line_jump_requested and line_focus is None)
    message = ''
    if not message and auto_message:
        message = auto_message
    return template_response(request, 'files.html', {'ctx': ctx, 'files': files, 'files_truncated': files_truncated, 'file_limit': _C.WORKSPACE_FILE_LIST_LIMIT, 'selected': selected, 'content': content, 'content_truncated': content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'selected_line': selected_line, 'selected_parent': selected_parent, 'browse_dir': browse_dir, 'browse_parent': browse_parent, 'browse_dirs': browse_dirs, 'browse_files': browse_files, 'browse_total': browse_total, 'browse_query_tail': browse_query_tail, 'line_focus': line_focus, 'line_jump_requested': line_jump_requested, 'line_jump_missing': line_jump_missing, 'selected_missing': selected_missing, 'selected_is_dir': selected_is_dir, 'selected_is_binary': selected_is_binary, 'selected_is_pdf': selected_is_pdf, 'selected_media_type': selected_media_type, 'selected_template_kind': selected_template_kind, 'message': message})

def files_save(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    content: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        saved_path = config.workspace_file_service.write_text(workspace, path, content, require_allowed_root=False)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.save', {'path': saved_path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(problem, user, path=path, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message=msg,
    )

def files_new(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'created'
    try:
        created_path = config.workspace_file_service.create_empty(workspace, path, require_allowed_root=False)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.new', {'path': created_path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(problem, user, path=path, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message=msg,
    )

def files_create_template(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'template created'
    try:
        expected_kind = kind_for_path(path)
        if not expected_kind:
            raise ValueError('template is only available for checker/interactor/validator/accepted solution')
        if expected_kind != kind:
            raise ValueError('template kind/path mismatch')
        content = template_for_kind(kind)
        with config.workspace_service.workspace_lock(workspace):
            abs_path = safe_workspace_path(workspace, path)
            if abs_path.exists() and abs_path.is_dir():
                raise ValueError('template target must be a file path')
            if abs_path.exists() and abs_path.is_file() and (abs_path.stat().st_size > 0):
                msg = 'file already exists; not overwritten'
            else:
                config.git_service.write_file(workspace, path, content)
                if expected_kind == 'solution' and ensure_solution_metadata_for_source(workspace, path):
                    msg = 'template and metadata created'
                audit(ctx['user']['id'], ctx['problem']['id'], 'files.create_template', {'path': path, 'kind': kind})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(problem, user, path=path, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message=msg,
    )

async def files_upload(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    upload: Annotated[UploadFile, File(...)],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    try:
        uploaded_path, total_bytes = await config.workspace_file_service.upload_file(
            workspace,
            path,
            upload,
            require_allowed_root=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    audit(ctx['user']['id'], ctx['problem']['id'], 'files.upload', {'path': uploaded_path, 'bytes': total_bytes})
    return redirect_response(
        _files_redirect_href(problem, user, path=path, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message='uploaded',
    )

def files_rename(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    old_path: Annotated[str, Form()],
    new_path: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = new_path
    msg = 'renamed'
    try:
        old_path, selected = config.workspace_file_service.rename_path(
            workspace,
            old_path,
            new_path,
            require_allowed_root=False,
        )
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.rename', {'old': old_path, 'new': selected})
    except (ValueError, OSError) as exc:
        selected = old_path
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(problem, user, path=selected, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message=msg,
    )

def files_delete(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'deleted'
    try:
        deleted_path = config.workspace_file_service.delete_path(workspace, path, require_allowed_root=False)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.delete', {'path': deleted_path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(problem, user, browse_tail=files_browse_query_tail(dir)),
        status_code=303,
        message=msg,
    )

def files_download(problem: str, user: Annotated[str, Depends(require_session_user)], path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    try:
        file_path = config.workspace_file_service.download_path(workspace, path, require_allowed_root=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='file not found')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(file_path, filename=file_path.name)





