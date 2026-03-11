from __future__ import annotations

import mimetypes
import os
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.impl.auth.public import redirect_response, template_response
from app.impl.runtime.config import config
from app.impl.problem.shared import _looks_like_binary_file
from app.impl.workspace.public import (
    audit,
    build_line_focus_context,
    build_repo_browser_entries,
    default_files_selected_path,
    ensure_solution_metadata_for_source,
    files_back_target,
    files_browse_query_tail,
    files_source_query_tail,
    kind_for_path,
    normalize_files_source,
    normalize_source_id,
    parse_line_param,
    require_write_access,
    template_for_kind,
    page_ctx,
)
from app.main_util import normalize_workspace_rel_path, safe_workspace_path

_C = config.constants

def files_page(request: Request, problem: str, user: str):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    selected = normalize_workspace_rel_path(request.query_params.get('path'))
    line_raw = request.query_params.get('line')
    selected_line = parse_line_param(line_raw, default=1)
    source_page = normalize_files_source(request.query_params.get('src'))
    source_id = normalize_source_id(request.query_params.get('sid'))
    source_back_href, source_back_label = files_back_target(problem, user, source_page, source_id)
    source_query_tail = files_source_query_tail(source_page, source_id)
    content = ''
    content_truncated = False
    selected_missing = False
    selected_is_dir = False
    selected_is_binary = False
    selected_is_pdf = False
    selected_media_type = ''
    auto_message = ''
    files, files_truncated = config.git_service.list_files_capped(workspace, limit=_C.WORKSPACE_FILE_LIST_LIMIT)
    default_selected = default_files_selected_path(workspace, files)
    if not selected:
        selected = default_selected
    try:
        selected_abs = safe_workspace_path(workspace, selected)
    except HTTPException:
        selected = default_selected
        selected_abs = safe_workspace_path(workspace, selected)
        auto_message = f'invalid path; opened {selected}'
    if selected_abs.exists() and selected_abs.is_file():
        selected_media_type = str(mimetypes.guess_type(selected)[0] or '')
        selected_is_pdf = selected.lower().endswith('.pdf') or selected_media_type == 'application/pdf'
        selected_is_binary = selected_is_pdf or _looks_like_binary_file(selected_abs)
        if not selected_is_binary:
            content, content_truncated = config.git_service.read_file_limited(workspace, selected, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    elif selected_abs.exists() and selected_abs.is_dir():
        selected_is_dir = True
    else:
        selected_missing = True
    selected_template_kind = kind_for_path(selected)
    selected_parent = str(Path(selected).parent)
    if selected_parent in {'.', ''}:
        selected_parent = ''
    requested_dir = request.query_params.get('dir')
    browse_dir_default = ''
    browse_dir_raw = requested_dir if requested_dir is not None else browse_dir_default
    browse_dir, browse_parent, browse_dirs, browse_files, browse_total = build_repo_browser_entries(workspace, files, str(browse_dir_raw or ''))
    browse_query_tail = files_browse_query_tail(browse_dir)
    line_focus = build_line_focus_context(content, selected_line) if line_raw else None
    line_jump_requested = bool(line_raw)
    line_jump_missing = bool(line_jump_requested and line_focus is None)
    message = ''
    if not message and auto_message:
        message = auto_message
    return template_response(request, 'files.html', {'ctx': ctx, 'files': files, 'files_truncated': files_truncated, 'file_limit': _C.WORKSPACE_FILE_LIST_LIMIT, 'selected': selected, 'content': content, 'content_truncated': content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'selected_line': selected_line, 'selected_parent': selected_parent, 'browse_dir': browse_dir, 'browse_parent': browse_parent, 'browse_dirs': browse_dirs, 'browse_files': browse_files, 'browse_total': browse_total, 'browse_query_tail': browse_query_tail, 'line_focus': line_focus, 'line_jump_requested': line_jump_requested, 'line_jump_missing': line_jump_missing, 'selected_missing': selected_missing, 'selected_is_dir': selected_is_dir, 'selected_is_binary': selected_is_binary, 'selected_is_pdf': selected_is_pdf, 'selected_media_type': selected_media_type, 'selected_template_kind': selected_template_kind, 'source_page': source_page, 'source_id': source_id, 'source_query_tail': source_query_tail, 'source_back_href': source_back_href, 'source_back_label': source_back_label, 'message': message})

def files_save(problem: str, user: str, path: str=Form(...), content: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    msg = 'saved'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.write_file(workspace, path, content)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.save', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

def files_new(problem: str, user: str, path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    msg = 'created'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.write_file(workspace, path, '')
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.new', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

def files_create_template(problem: str, user: str, path: str=Form(...), kind: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    msg = 'template created'
    try:
        expected_kind = kind_for_path(path)
        if not expected_kind:
            raise ValueError('template is only available for checker/interactor/validator/accepted solution')
        if expected_kind != str(kind).strip().lower():
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
    return redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message=msg)

async def files_upload(problem: str, user: str, path: str=Form(...), upload: UploadFile=File(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    total_bytes = 0
    tmp_path: Path | None = None
    try:
        with config.workspace_service.workspace_lock(workspace):
            abs_path = safe_workspace_path(workspace, path)
            if abs_path.exists() and abs_path.is_dir():
                raise HTTPException(status_code=400, detail='upload target must be a file path')
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.upload-{abs_path.name}.', suffix='.tmp', dir=str(abs_path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, 'wb') as out:
                    while True:
                        chunk = await upload.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        total_bytes += len(chunk)
                os.replace(tmp_path, abs_path)
                tmp_path = None
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                    tmp_path = None
                raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    audit(ctx['user']['id'], ctx['problem']['id'], 'files.upload', {'path': path, 'bytes': total_bytes})
    return redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(path)}{tail}', status_code=303, message='uploaded')

def files_rename(problem: str, user: str, old_path: str=Form(...), new_path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    selected = new_path
    msg = 'renamed'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.rename_path(workspace, old_path, new_path)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.rename', {'old': old_path, 'new': new_path})
    except (ValueError, OSError) as exc:
        selected = old_path
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/files?path={quote_plus(selected)}{tail}', status_code=303, message=msg)

def files_delete(problem: str, user: str, path: str=Form(...), dir: str=Form(''), src: str=Form(''), sid: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    source_page = normalize_files_source(src)
    source_id = normalize_source_id(sid)
    tail = files_browse_query_tail(dir) + files_source_query_tail(source_page, source_id)
    msg = 'deleted'
    try:
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.delete_path(workspace, path)
        audit(ctx['user']['id'], ctx['problem']['id'], 'files.delete', {'path': path})
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/files{tail}', status_code=303, message=msg)

def files_download(problem: str, user: str, path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    file_path = safe_workspace_path(workspace, path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail='file not found')
    return FileResponse(file_path, filename=file_path.name)





