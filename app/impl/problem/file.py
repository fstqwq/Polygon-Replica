from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.workspace_scope import (
    contest_workspace_context_from_request,
    problem_href_builder,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_operation import (
    build_line_focus_context,
    build_repo_browser_context,
    kind_for_path,
    parse_line_param,
    template_for_kind,
)
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.solution import ensure_solution_metadata_for_source
from app.main_util import (
    normalize_workspace_rel_path,
    problem_slug_leaf,
    safe_workspace_path,
)
from app.service.statement.constant import STATEMENT_DEFAULT_FILES



def _files_redirect_href(
    request: Request,
    problem: str,
    *,
    path: str = '',
    browse_dir: str = '',
) -> str:
    query: dict[str, str] = {}
    if path:
        query['path'] = path
    if browse_dir:
        query['dir'] = browse_dir
    return problem_href_builder(request, problem)(
        'problem_files',
        query=query or None,
    )


def _files_write_context(request: Request, problem: str, user: str) -> dict:
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=False,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    require_write_access(ctx)
    return ctx


def _repository_parent(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return '' if parent == '.' else parent


def files_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    selected = normalize_workspace_rel_path(request.query_params.get('path'))
    requested_dir = request.query_params.get('dir')
    line_raw = request.query_params.get('line')
    selected_line = parse_line_param(line_raw, default=1)
    content = ''
    content_truncated = False
    selected_missing = False
    selected_is_binary = False
    selected_is_pdf = False
    selected_media_type = ''
    auto_message = ''
    files, files_truncated = runtime().workspace_file_service.list_paths(
        workspace,
        limit=runtime().config_values.WORKSPACE_FILE_LIST_LIMIT,
        require_allowed_root=False,
    )
    if requested_dir is None and selected:
        requested_dir = _repository_parent(selected)
    browser = build_repo_browser_context(
        workspace,
        files,
        requested_dir or '',
        root_label=problem_slug_leaf(ctx['problem']['slug']),
    )
    if selected:
        try:
            selected = runtime().workspace_file_service.normalize_path(
                selected,
                require_allowed_root=False,
            )
        except (HTTPException, ValueError):
            selected = ''
            auto_message = 'invalid path'
    if selected and _repository_parent(selected) != browser['directory']:
        selected = ''
    if selected:
        selected_view = runtime().workspace_file_service.file_view(
            workspace,
            selected,
            char_limit=runtime().config_values.WORKSPACE_FILE_VIEW_CHAR_LIMIT,
            require_allowed_root=False,
        )
        if selected_view.is_dir:
            selected = ''
        else:
            selected_missing = not selected_view.exists
            selected_is_binary = selected_view.is_binary
            selected_is_pdf = selected_view.is_pdf
            selected_media_type = selected_view.media_type
            content = selected_view.content
            content_truncated = selected_view.content_truncated
    selected_can_restore_default = (
        bool(selected)
        and selected in STATEMENT_DEFAULT_FILES
        and (selected_missing or not selected_is_binary)
    )
    selected_template_kind = kind_for_path(selected) if selected else ''
    line_focus = build_line_focus_context(content, selected_line) if line_raw else None
    line_jump_requested = bool(line_raw)
    line_jump_missing = bool(line_jump_requested and line_focus is None)
    message = ''
    if not message and auto_message:
        message = auto_message
    template_context = {
        'ctx': ctx,
        'files': files,
        'files_truncated': files_truncated,
        'file_limit': runtime().config_values.WORKSPACE_FILE_LIST_LIMIT,
        'selected': selected,
        'selected_name': PurePosixPath(selected).name if selected else '',
        'content': content,
        'content_truncated': content_truncated,
        'content_char_limit': runtime().config_values.WORKSPACE_FILE_VIEW_CHAR_LIMIT,
        'selected_line': selected_line,
        'browser': browser,
        'line_focus': line_focus,
        'line_jump_requested': line_jump_requested,
        'line_jump_missing': line_jump_missing,
        'selected_missing': selected_missing,
        'selected_is_binary': selected_is_binary,
        'selected_is_pdf': selected_is_pdf,
        'selected_media_type': selected_media_type,
        'selected_template_kind': selected_template_kind,
        'selected_can_restore_default': selected_can_restore_default,
        'message': message,
    }
    return template_response(request, 'files.html', template_context)

def files_save(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    content: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        runtime().workspace_file_service.write_text(workspace, path, content, require_allowed_root=False)
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(request, problem, path=path, browse_dir=dir),
        status_code=303,
        message=msg,
    )

def files_new(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    name: Annotated[str, Form()],
    selected: Annotated[str, Form()] = '',
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    msg = 'created'
    selected_path = selected
    try:
        path = runtime().workspace_file_service.child_path(
            workspace,
            dir,
            name,
            require_allowed_root=False,
        )
        created_path = runtime().workspace_file_service.create_empty(
            workspace,
            path,
            require_allowed_root=False,
        )
        selected_path = created_path
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(
            request,
            problem,
            path=selected_path,
            browse_dir=dir,
        ),
        status_code=303,
        message=msg,
    )


def files_new_directory(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    name: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    msg = 'directory created'
    browse_dir = dir
    try:
        path = runtime().workspace_file_service.child_path(
            workspace,
            dir,
            name,
            require_allowed_root=False,
        )
        browse_dir = runtime().workspace_file_service.create_directory(
            workspace,
            path,
            require_allowed_root=False,
        )
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(request, problem, browse_dir=browse_dir),
        status_code=303,
        message=msg,
    )

def files_create_template(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    kind: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    msg = 'template created'
    try:
        expected_kind = kind_for_path(path)
        if not expected_kind:
            raise ValueError('template is only available for checker/interactor/validator/accepted solution')
        if expected_kind != kind:
            raise ValueError('template kind/path mismatch')
        content = template_for_kind(kind)
        with runtime().workspace_service.workspace_lock(workspace):
            abs_path = safe_workspace_path(workspace, path)
            if abs_path.exists() and abs_path.is_dir():
                raise ValueError('template target must be a file path')
            if abs_path.exists() and abs_path.is_file() and (abs_path.stat().st_size > 0):
                msg = 'file already exists; not overwritten'
            else:
                runtime().git_service.write_file(workspace, path, content)
                if expected_kind == 'solution' and ensure_solution_metadata_for_source(workspace, path):
                    msg = 'template and metadata created'
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(request, problem, path=path, browse_dir=dir),
        status_code=303,
        message=msg,
    )


def files_restore_default(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    selected = path
    message = 'default statement file restored'
    try:
        selected = runtime().workspace_file_service.normalize_path(
            path,
            require_allowed_root=False,
        )
        content = STATEMENT_DEFAULT_FILES.get(selected)
        if content is None:
            raise ValueError('default restore is not available for this path')
        with runtime().workspace_service.workspace_lock(workspace):
            runtime().git_service.write_file(workspace, selected, content)
    except (ValueError, OSError) as exc:
        message = str(exc)
    return redirect_response(
        _files_redirect_href(
            request,
            problem,
            path=selected,
            browse_dir=dir,
        ),
        status_code=303,
        message=message,
    )


async def files_upload(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    upload: Annotated[UploadFile, File(...)],
    path: Annotated[str, Form()] = '',
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    message = 'uploaded'
    selected = path
    try:
        if not selected:
            upload_name = str(upload.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
            if not upload_name or upload_name in {'.', '..'}:
                raise ValueError('uploaded file name is required')
            selected = f'{dir}/{upload_name}' if dir else upload_name
        uploaded_path, total_bytes = await runtime().workspace_file_service.upload_file(
            workspace,
            selected,
            upload,
            require_allowed_root=False,
        )
    except (ValueError, OSError) as exc:
        message = str(exc)
    finally:
        await upload.close()
    return redirect_response(
        _files_redirect_href(request, problem, path=selected, browse_dir=dir),
        status_code=303,
        message=message,
    )

def files_rename(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    old_path: Annotated[str, Form()],
    new_name: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    selected = old_path
    msg = 'renamed'
    try:
        normalized_old_path = runtime().workspace_file_service.normalize_path(
            old_path,
            require_allowed_root=False,
        )
        normalized_dir = runtime().workspace_file_service.normalize_path(
            dir,
            allow_empty=True,
            require_allowed_root=False,
        )
        if _repository_parent(normalized_old_path) != normalized_dir:
            raise ValueError('selected file is not in the current folder')
        new_path = runtime().workspace_file_service.child_path(
            workspace,
            normalized_dir,
            new_name,
            require_allowed_root=False,
        )
        old_path, selected = runtime().workspace_file_service.rename_path(
            workspace,
            normalized_old_path,
            new_path,
            require_allowed_root=False,
        )
    except (ValueError, OSError) as exc:
        selected = old_path
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(request, problem, path=selected, browse_dir=dir),
        status_code=303,
        message=msg,
    )

def files_delete(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    path: Annotated[str, Form()],
    dir: Annotated[str, Form()] = '',
):
    ctx = _files_write_context(request, problem, user)
    workspace = Path(ctx['workspace']['path'])
    msg = 'deleted'
    try:
        runtime().workspace_file_service.delete_path(workspace, path, require_allowed_root=False)
    except ValueError as exc:
        msg = str(exc)
    return redirect_response(
        _files_redirect_href(request, problem, browse_dir=dir),
        status_code=303,
        message=msg,
    )

def files_download(problem: str, user: Annotated[str, Depends(require_session_user)], path: str):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    workspace = Path(ctx['workspace']['path'])
    try:
        file_path = runtime().workspace_file_service.download_path(workspace, path, require_allowed_root=False)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='file not found')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(file_path, filename=file_path.name)
