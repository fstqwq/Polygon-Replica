from __future__ import annotations
from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request, Depends
from fastapi.responses import JSONResponse

from app.impl.auth.shared import redirect_response, set_flash_cookie, template_response
from app.impl.contest.workspace_scope import contest_workspace_context_from_request
from app.impl.runtime.config import config
from app.impl.problem.shared import MAIN_CORRECT_EXPECTED_LABEL, MAIN_CORRECT_EXPECTED_VALUE
from app.impl.workspace.context_operation import audit, list_solution_entries, normalize_optional_component_source_path_safe, read_build_config, read_text_safe_limited, resolve_build_accepted_solution_source, solution_metadata_entry, workspace_rel_file_exists, write_build_config
from app.impl.workspace.solution import normalize_solution_source_path_required, solution_behavior_options
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.main_util import enforce_textarea_max_bytes
from app.service.problem.solution_metadata import (
    desc_rel_path_for_source,
    normalize_expected_behavior,
    parse_solution_desc,
    render_solution_desc,
)
from app.service.platform.workspace_path import (
    normalize_component_source_path,
    normalize_workspace_rel_path,
    safe_workspace_path,
)

_C = config.constants


def solutions_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    entries, entries_truncated = list_solution_entries(workspace)
    selected = normalize_workspace_rel_path(request.query_params.get('path'))
    if not selected or not any(row.get('source_path') == selected for row in entries):
        selected = entries[0]['source_path'] if entries else ''
    selected_entry = next((row for row in entries if row.get('source_path') == selected), None)
    accepted_source = resolve_build_accepted_solution_source(workspace, entries)
    accepted_source_exists = bool(accepted_source) and workspace_rel_file_exists(workspace, accepted_source)
    expected_behavior_options = [{'value': MAIN_CORRECT_EXPECTED_VALUE, 'label': MAIN_CORRECT_EXPECTED_LABEL}, *solution_behavior_options()]
    entries_view: list[dict] = []
    for row in entries:
        row_view = dict(row)
        source_path = row_view['source_path']
        raw_expected = normalize_expected_behavior(row_view['expected_behavior'])
        effective_expected = raw_expected
        # Main correct solution is controlled by build config and must be unique in UI.
        if accepted_source_exists and source_path == accepted_source:
            effective_expected = MAIN_CORRECT_EXPECTED_VALUE
        row_view['expected_behavior_effective'] = effective_expected
        entries_view.append(row_view)
    existing_paths = {str(row['source_path']) for row in entries}
    solution_create_default_path = 'solutions/accepted.cpp'
    if solution_create_default_path in existing_paths:
        suffix = 1
        solution_create_default_path = 'solutions/solution.cpp'
        while solution_create_default_path in existing_paths:
            suffix += 1
            solution_create_default_path = f'solutions/solution_{suffix}.cpp'
    return template_response(request, 'solutions.html', {'ctx': ctx, 'entries': entries_view, 'entries_truncated': entries_truncated, 'entries_limit': _C.SOLUTION_LIST_LIMIT, 'selected': selected, 'selected_entry': selected_entry, 'expected_behavior_options': expected_behavior_options, 'accepted_source': accepted_source, 'accepted_source_exists': accepted_source_exists, 'solution_create_default_path': solution_create_default_path})

def solutions_editor_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    entries, entries_truncated = list_solution_entries(workspace)
    requested = normalize_workspace_rel_path(request.query_params.get('path'))
    selected = ''
    if requested:
        try:
            selected = normalize_solution_source_path_required(requested)
        except ValueError:
            selected = ''
    if not selected:
        selected = entries[0]['source_path'] if entries else 'solutions/accepted.cpp'
    selected_entry = next((row for row in entries if row.get('source_path') == selected), None)
    selected_exists = False
    content = ''
    content_truncated = False
    try:
        selected_abs = safe_workspace_path(workspace, selected)
        if selected_abs.exists() and selected_abs.is_file() and (not selected_abs.is_symlink()):
            selected_exists = True
            content, content_truncated = config.git_service.read_file_limited(workspace, selected, _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT)
    except HTTPException:
        selected_exists = False
        content = ''
        content_truncated = False
    if selected_entry is None:
        selected_entry = solution_metadata_entry(workspace, selected)
    return template_response(request, 'solutions_editor.html', {'ctx': ctx, 'entries': entries, 'entries_truncated': entries_truncated, 'entries_limit': _C.SOLUTION_LIST_LIMIT, 'selected': selected, 'selected_entry': selected_entry, 'selected_exists': selected_exists, 'content': content, 'content_truncated': content_truncated, 'content_char_limit': _C.WORKSPACE_FILE_VIEW_CHAR_LIMIT, 'expected_behavior_options': solution_behavior_options()})

def solutions_save_source(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)], source_path: str=Form(...), content: str=Form(''), expected_behavior: str=Form('unknown')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = 'solutions/accepted.cpp'
    selected_for_redirect = selected
    msg = 'solution source saved'
    save_ok = False
    metadata_created = False
    normalized_expected = 'unknown'
    requested_with = request.headers.get('x-requested-with')
    json_requested = requested_with is not None and requested_with.strip().lower() in {'fetch', 'xmlhttprequest'}
    if not json_requested:
        accept = request.headers.get('accept', '').lower()
        json_requested = 'application/json' in accept
    try:
        selected = normalize_solution_source_path_required(source_path)
        normalized_expected = normalize_expected_behavior(expected_behavior)
        safe_content = enforce_textarea_max_bytes(content, label='solution source')
        selected_for_redirect = selected
        with config.workspace_service.workspace_lock(workspace):
            desc_path = desc_rel_path_for_source(selected)
            desc_abs = safe_workspace_path(workspace, desc_path)
            desc_existed_before = desc_abs.exists() and desc_abs.is_file() and (not desc_abs.is_symlink())
            desc_note = ''
            if desc_existed_before:
                desc_text, _ = read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
                parsed_desc = parse_solution_desc(desc_text)
                note = parsed_desc.get('note')
                if isinstance(note, str):
                    desc_note = note
            config.git_service.write_file(workspace, selected, safe_content)
            config.git_service.write_file(workspace, desc_path, render_solution_desc(normalized_expected, desc_note))
            metadata_created = not desc_existed_before
        if metadata_created:
            msg = 'solution source and metadata saved'
        save_ok = True
        audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.save_source', {'path': selected, 'bytes': len(safe_content.encode('utf-8')), 'metadata_created': metadata_created, 'expected_behavior': normalized_expected})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    editor_url = f'/problems/{problem}/solutions/editor?path={quote_plus(selected_for_redirect)}'
    if json_requested:
        if save_ok:
            response = JSONResponse({'ok': True, 'redirect': editor_url, 'message': msg})
            set_flash_cookie(response, [msg])
            return response
        return JSONResponse({'ok': False, 'error': msg}, status_code=400)
    return redirect_response(editor_url, status_code=303, message=msg)

def solutions_set_tag(problem: str, user: Annotated[str, Depends(require_session_user)], source_path: str=Form(...), expected_behavior: str=Form('unknown')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = 'solutions/accepted.cpp'
    msg = 'solution tag updated'
    try:
        selected = normalize_solution_source_path_required(source_path)
        source_abs = safe_workspace_path(workspace, selected)
        if source_abs.is_symlink() or not source_abs.exists() or (not source_abs.is_file()):
            raise ValueError('solution source does not exist')
        raw_expected = expected_behavior.strip().lower()
        is_main_correct = raw_expected in {MAIN_CORRECT_EXPECTED_VALUE, 'main-correct', 'maincorrect'}
        normalized_expected = 'accepted' if is_main_correct else normalize_expected_behavior(expected_behavior)
        desc_path = desc_rel_path_for_source(selected)
        note = ''
        with config.workspace_service.workspace_lock(workspace):
            if workspace_rel_file_exists(workspace, desc_path):
                desc_abs = safe_workspace_path(workspace, desc_path)
                desc_text, _ = read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
                parsed = parse_solution_desc(desc_text)
                parsed_note = parsed.get('note')
                if isinstance(parsed_note, str):
                    note = parsed_note
            build_cfg, cfg_path = read_build_config(workspace)
            configured = normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
            build_cfg_changed = False
            if is_main_correct:
                if configured != selected:
                    build_cfg['accepted_solution_source'] = selected
                    build_cfg_changed = True
            elif configured == selected:
                build_cfg.pop('accepted_solution_source', None)
                build_cfg_changed = True
            if build_cfg_changed:
                write_build_config(cfg_path, build_cfg)
            if normalized_expected == 'unknown' and (not note):
                config.git_service.delete_path(workspace, desc_path)
                msg = 'solution tag cleared'
            else:
                config.git_service.write_file(workspace, desc_path, render_solution_desc(normalized_expected, note))
                if is_main_correct:
                    msg = 'solution tag set to main correct solution (AC)'
                else:
                    msg = f'solution tag set to {normalized_expected}'
        audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.set_tag', {'source': selected, 'expected_behavior': normalized_expected, 'main_correct': bool(is_main_correct)})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/solutions?path={quote_plus(selected)}', status_code=303, message=msg)

def solutions_rename(problem: str, user: Annotated[str, Depends(require_session_user)], old_path: str=Form(...), new_path: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = normalize_workspace_rel_path(old_path)
    msg = 'solution renamed'
    try:
        old_source = normalize_solution_source_path_required(old_path)
        if not (new_source := normalize_workspace_rel_path(new_path)):
            raise ValueError('new solution source is required')
        if not new_source.startswith('solutions/'):
            new_source = f'solutions/{new_source}'
        new_source = normalize_component_source_path(new_source, 'solutions', 'accepted.cpp')
        selected = old_source
        if old_source == new_source:
            msg = 'solution rename skipped'
        else:
            old_desc = desc_rel_path_for_source(old_source)
            new_desc = desc_rel_path_for_source(new_source)
            renamed_metadata = False
            with config.workspace_service.workspace_lock(workspace):
                old_abs = safe_workspace_path(workspace, old_source)
                if old_abs.is_symlink() or (not old_abs.exists()) or (not old_abs.is_file()):
                    raise ValueError('solution source does not exist')
                new_abs = safe_workspace_path(workspace, new_source)
                if new_abs.exists():
                    raise ValueError('destination source already exists')
                old_desc_exists = workspace_rel_file_exists(workspace, old_desc)
                if old_desc_exists and workspace_rel_file_exists(workspace, new_desc):
                    raise ValueError('destination metadata already exists')
                config.git_service.rename_path(workspace, old_source, new_source)
                if old_desc_exists and old_desc != new_desc:
                    config.git_service.rename_path(workspace, old_desc, new_desc)
                    renamed_metadata = True
                build_cfg, cfg_path = read_build_config(workspace)
                configured = normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
                if configured == old_source:
                    build_cfg['accepted_solution_source'] = new_source
                    write_build_config(cfg_path, build_cfg)
            selected = new_source
            audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.rename', {'old': old_source, 'new': new_source, 'renamed_metadata': renamed_metadata})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/solutions?path={quote_plus(selected)}', status_code=303, message=msg)

def solutions_delete(problem: str, user: Annotated[str, Depends(require_session_user)], source_path: str=Form(...)):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    selected = normalize_workspace_rel_path(source_path)
    msg = 'solution deleted'
    try:
        selected = normalize_solution_source_path_required(source_path)
        desc_path = desc_rel_path_for_source(selected)
        with config.workspace_service.workspace_lock(workspace):
            source_abs = safe_workspace_path(workspace, selected)
            if source_abs.is_symlink() or (not source_abs.exists()) or (not source_abs.is_file()):
                raise ValueError('solution source does not exist')
            config.git_service.delete_path(workspace, selected)
            if workspace_rel_file_exists(workspace, desc_path):
                config.git_service.delete_path(workspace, desc_path)
            build_cfg, cfg_path = read_build_config(workspace)
            configured = normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
            if configured == selected:
                build_cfg.pop('accepted_solution_source', None)
                write_build_config(cfg_path, build_cfg)
        audit(ctx['user']['id'], ctx['problem']['id'], 'solutions.delete', {'source': selected, 'desc': desc_path})
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(f'/problems/{problem}/solutions', status_code=303, message=msg)
