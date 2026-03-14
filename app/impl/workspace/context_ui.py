from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from app.impl.auth.public import has_sudo_session, template_response
from app.impl.runtime.config import config

from app.main_util import normalize_workspace_rel_path
from app.service.statement.signature import statement_sources_signature

from .access import (
    problem_acl_entries,
    require_read_access,
    workspace_access_context,
)
from .artifact import artifact_version_number, safe_artifact_path
from .context import count_label
from .context_operation import (
    _normalize_standard_checker_name,
    parse_summary_json,
    _solutions_status_context,
    _tests_spec_status_context,
)
from .context_component_status import (
    checker_status_context,
    _count_used_configured_generators,
    generator_status_context,
    interactor_status_context,
    validator_status_context,
)
from .context_verification import _verification_status_context
from .problem_config import (
    coerce_int,
    normalize_problem_mode,
    read_problem_config,
)
from .revision import git_commit_count, workspace_revision_info

_C = config.constants
def page_ctx(problem: str, user: str, include_branches: bool=True, refresh_status: bool=True, include_recent: bool=True, include_workspace_changes: bool=True) -> dict:
    _ = include_branches
    try:
        problem_row = config.db.fetch_one('SELECT id FROM problems WHERE slug=?', [problem])
        if problem_row is None:
            raise ValueError(f'Unknown problem: {problem}')
        user_row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [user])
        if user_row is None:
            raise ValueError(f'Unknown user: {user}')
        access = workspace_access_context(int(problem_row['id']), int(user_row['id']))
        require_read_access({'access': access})
        if refresh_status:
            # Refresh context status from git without persisting workspace status into DB.
            config.workspace_service.ensure_workspace(problem, user, refresh_status=False)
        ctx = config.workspace_service.workspace_context(problem, user, include_recent=include_recent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    ctx['access'] = access
    ctx['branches'] = ['main']
    ctx['branches_truncated'] = False
    ctx['branch_limit'] = 1
    workspace_path = Path(ctx['workspace']['path'])
    if refresh_status:
        live_status: dict | None = None
        try:
            with config.workspace_service.workspace_lock(workspace_path):
                status_obj = config.workspace_service.read_workspace_status(workspace_path)
            if isinstance(status_obj, dict):
                live_status = status_obj
        except Exception:
            live_status = None
        if isinstance(live_status, dict):
            ctx['workspace']['branch'] = str(live_status.get('branch') or 'main').strip() or 'main'
            ctx['workspace']['head_commit'] = str(live_status.get('head_commit') or '').strip()
            ctx['workspace']['dirty'] = 1 if bool(live_status.get('dirty')) else 0
    workspace_branch = str(ctx['workspace'].get('branch') or 'main').strip() or 'main'
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    try:
        _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
        safe_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        ctx['problem_mode'] = safe_mode
        ctx['general_cfg'] = {'time_limit_ms': coerce_int(general_cfg.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS), 'memory_limit_mb': coerce_int(general_cfg.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB), 'mode': safe_mode}
    except Exception:
        ctx['problem_mode'] = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
        ctx['general_cfg'] = {'time_limit_ms': int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), 'memory_limit_mb': int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), 'mode': str(_C.GENERAL_CONFIG_DEFAULTS['mode'])}
    ctx['workspace_revision'] = workspace_revision_info(
        workspace_path,
        workspace_branch,
        workspace_head=workspace_head,
        workspace_dirty=workspace_dirty,
    )
    ctx['workspace_version'] = ctx['workspace_revision']['local']
    ctx['workspace_upstream_version'] = ctx['workspace_revision']['upstream']
    ctx['workspace_revision_alert'] = bool(ctx['workspace_revision']['highlight'])
    behind_count_raw = ctx['workspace_revision'].get('behind_count')
    behind_count = 0
    try:
        if behind_count_raw is not None:
            behind_count = max(0, int(behind_count_raw))
    except Exception:
        behind_count = 0
    ctx['workspace_needs_update'] = bool(ctx['workspace_revision'].get('upstream_higher')) or behind_count > 0
    ctx['head_short'] = workspace_head[:8]
    try:
        ctx['checker_status'] = checker_status_context(workspace_path)
    except Exception:
        ctx['checker_status'] = {'mode': 'missing', 'display': 'unknown', 'standard_checker': '', 'standard_valid': False, 'repo_source': 'checkers/checker.cpp', 'repo_source_exists': False}
    try:
        ctx['generator_status'] = generator_status_context(workspace_path)
    except Exception:
        ctx['generator_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'generators/generator.cpp', 'repo_source_exists': False, 'configured_sources': [], 'source_rows_truncated': False}
    try:
        ctx['interactor_status'] = interactor_status_context(workspace_path)
    except Exception:
        ctx['interactor_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'interactors/interactor.cpp', 'repo_source_exists': False}
    try:
        ctx['validator_status'] = validator_status_context(workspace_path)
    except Exception:
        ctx['validator_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'validators/validator.cpp', 'repo_source_exists': False}
    try:
        ctx['solutions_status'] = _solutions_status_context(workspace_path)
    except Exception:
        ctx['solutions_status'] = {'mode': 'missing', 'display': 'missing', 'accepted_source': '', 'accepted_exists': False, 'count': 0, 'count_display': '0 files', 'truncated': False}
    try:
        ctx['tests_spec_status'] = _tests_spec_status_context(workspace_path)
    except Exception:
        ctx['tests_spec_status'] = {'mode': 'invalid', 'display': 'invalid', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    empty_changes = {'counts': {'added': 0, 'modified': 0, 'deleted': 0, 'renamed': 0, 'untracked': 0, 'conflicted': 0, 'typechange': 0, 'other': 0}, 'rows': [], 'total': 0, 'truncated': False, 'limit': None}
    if include_workspace_changes:
        try:
            ctx['workspace_changes'] = config.git_service.status_change_summary(workspace_path)
        except Exception:
            ctx['workspace_changes'] = empty_changes
    else:
        ctx['workspace_changes'] = empty_changes
    try:
        ctx['verification_status'] = _verification_status_context(
            int(ctx['problem']['id']),
            int(ctx['user']['id']),
            int(ctx['workspace']['id']),
            workspace_head,
            workspace_dirty,
            workspace_path=workspace_path,
        )
    except Exception:
        ctx['verification_status'] = {
            'mode': 'none',
            'display': 'none',
            'last_status': 'none',
            'run_id': '',
            'run_ids': '',
            'artifact_verification_id': '',
            'error': '',
            'created_at': '',
            'stale': False,
            'stale_reason': '',
        }
    latest_verification = ctx.get('latest_artifact_verification')
    latest_preview = ctx.get('latest_preview')
    ctx['latest_verification_version'] = artifact_version_number(latest_verification['id']) if latest_verification else None
    ctx['latest_preview_version'] = artifact_version_number(latest_preview['id']) if latest_preview else None
    ctx['nav_status'] = _build_problem_nav_status(ctx)
    return ctx

def _build_problem_nav_status(ctx: dict) -> dict[str, dict[str, object]]:

    def _to_int(value: object, default: int=0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _row_value(row: object, key: str, default: object='') -> object:
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default

    def _short_decimal(value: float) -> str:
        text = f'{float(value):.3f}'.rstrip('0').rstrip('.')
        return text or '0'

    def _compact_time_limit_label(ms_value: int) -> str:
        ms = max(0, int(ms_value))
        ms_text = f'{ms}ms'
        sec_text = f'{_short_decimal(ms / 1000.0)}s'
        return sec_text if len(sec_text) < len(ms_text) else ms_text

    def _compact_memory_limit_label(mb_value: int) -> str:
        mb = max(0, int(mb_value))
        mb_text = f'{mb}mb'
        gb_text = f'{_short_decimal(mb / 1024.0)}gb'
        return gb_text if len(gb_text) < len(mb_text) else mb_text

    nav: dict[str, dict[str, object]] = {}
    general_cfg = ctx.get('general_cfg') if isinstance(ctx, dict) else None
    time_limit_ms = _to_int(general_cfg.get('time_limit_ms') if isinstance(general_cfg, dict) else int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']))
    memory_limit_mb = _to_int(general_cfg.get('memory_limit_mb') if isinstance(general_cfg, dict) else int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']))
    mode_text = normalize_problem_mode(general_cfg.get('mode') if isinstance(general_cfg, dict) else str(_C.GENERAL_CONFIG_DEFAULTS['mode']), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    time_text = _compact_time_limit_label(time_limit_ms)
    memory_text = _compact_memory_limit_label(memory_limit_mb)
    nav['general'] = {'text': f'{time_text}, {memory_text}, {mode_text}', 'danger': False}
    latest_preview = ctx.get('latest_preview')
    preview_status = str(_row_value(latest_preview, 'status', 'none') or 'none')
    preview_text = preview_status
    preview_danger = preview_status in {'none', 'missing', 'failed', 'error'}
    preview_warn = False
    if preview_status == 'ok':
        preview_id = str(_row_value(latest_preview, 'id', '') or '').strip()
        problem_slug = str(_row_value(ctx.get('problem'), 'slug', '') or '').strip()
        problem_id = _to_int(_row_value(ctx.get('problem'), 'id', 0))
        workspace_id = _to_int(_row_value(ctx.get('workspace'), 'id', 0))
        has_pdf = False
        if preview_id and problem_slug:
            try:
                safe_artifact_path(problem_slug, preview_id, 'statement_preview/statement.pdf')
                has_pdf = True
            except HTTPException:
                has_pdf = False
        if not has_pdf:
            preview_text = 'missing'
            preview_danger = True
        elif preview_id and problem_id > 0 and (workspace_id > 0):
            preview_row = config.db.fetch_one('SELECT source_commit,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [preview_id, problem_id, workspace_id])
            if preview_row is None:
                preview_text = 'missing'
                preview_danger = True
            else:
                preview_source_commit = str(_row_value(preview_row, 'source_commit', '') or '').strip()
                summary_obj = parse_summary_json(_row_value(preview_row, 'summary_json', None), f'preview/{preview_id}') or {}
                preview_signature = str(summary_obj.get('statement_signature') or '').strip() if isinstance(summary_obj, dict) else ''
                workspace_path_text = str(_row_value(ctx.get('workspace'), 'path', '') or '').strip()
                problem_title = str(_row_value(ctx.get('problem'), 'name', '') or '').strip()
                current_signature = ''
                if workspace_path_text:
                    try:
                        current_signature = statement_sources_signature(Path(workspace_path_text), problem_title=problem_title)
                    except Exception:
                        current_signature = ''
                workspace_head = str(_row_value(ctx.get('workspace'), 'head_commit', '') or '').strip()
                stale_by_signature = bool(preview_signature and current_signature and (preview_signature != current_signature))
                stale_by_head = bool((not preview_signature or not current_signature) and preview_source_commit and workspace_head and (preview_source_commit != workspace_head))
                if stale_by_signature or stale_by_head:
                    preview_text = 'stale'
                    preview_danger = False
                    preview_warn = True
    nav['preview'] = {'text': preview_text, 'danger': preview_danger, 'warn': preview_warn}
    workspace_changes = ctx.get('workspace_changes') if isinstance(ctx, dict) else None
    changes_total = _to_int(workspace_changes.get('total') if isinstance(workspace_changes, dict) else 0)
    nav['files'] = {'text': 'clean' if changes_total <= 0 else f'{changes_total} changed', 'danger': False}
    generator_status = ctx.get('generator_status') if isinstance(ctx, dict) else None
    generator_mode = str(generator_status.get('mode') or '') if isinstance(generator_status, dict) else ''
    configured_rows = generator_status.get('configured_sources') if isinstance(generator_status, dict) else []
    configured_count = 0
    configured_ready = 0
    configured_paths: list[str] = []
    source_paths: list[str] = []
    if isinstance(configured_rows, list):
        for row in configured_rows:
            if not isinstance(row, dict):
                continue
            row_path = str(row.get('path') or '').strip()
            if row_path:
                source_paths.append(row_path)
            if bool(row.get('configured')):
                configured_count += 1
                if row_path:
                    configured_paths.append(row_path)
                if bool(row.get('exists')):
                    configured_ready += 1
    if configured_count > 0:
        used_count = 0
        workspace_path_text = str(_row_value(ctx.get('workspace'), 'path', '') or '').strip()
        if workspace_path_text:
            try:
                used_count = _count_used_configured_generators(Path(workspace_path_text), configured_paths, source_paths)
            except Exception:
                used_count = 0
        generator_text = f'{count_label(configured_count, "file")}, {used_count} used'
        generator_danger = configured_ready < configured_count
    else:
        generator_text = str(generator_status.get('display') or 'missing') if isinstance(generator_status, dict) else 'missing'
        generator_danger = generator_mode in {'missing', 'invalid'}
    nav['generators'] = {'text': generator_text, 'danger': bool(generator_danger)}
    checker_status = ctx.get('checker_status') if isinstance(ctx, dict) else None
    checker_display = str(checker_status.get('display') or 'unknown') if isinstance(checker_status, dict) else 'unknown'
    checker_mode = str(checker_status.get('mode') or '') if isinstance(checker_status, dict) else ''
    checker_standard_invalid = bool(isinstance(checker_status, dict) and checker_mode == 'standard' and (not bool(checker_status.get('standard_valid'))))
    checker_hint = ''
    if isinstance(checker_status, dict) and checker_mode == 'standard':
        raw_standard = str(checker_status.get('standard_checker') or checker_display or '').strip()
        if raw_standard:
            canonical = raw_standard
            description = 'general-purpose standard checker from testlib'
            try:
                standard_name = _normalize_standard_checker_name(raw_standard)
                canonical = f'std::{standard_name}'
                description = str(_C.STANDARD_CHECKER_DESCRIPTIONS.get(standard_name, description))
            except ValueError:
                if not canonical.startswith('std::'):
                    canonical = f'std::{canonical}'
            checker_hint = f'{canonical} - {description}'
    nav['checker'] = {'text': checker_display, 'danger': checker_mode in {'missing', 'none'} or checker_display in {'unknown', 'error', 'missing'} or checker_standard_invalid, 'hint': checker_hint}
    interactor_status = ctx.get('interactor_status') if isinstance(ctx, dict) else None
    interactor_mode = str(interactor_status.get('mode') or '') if isinstance(interactor_status, dict) else ''
    interactor_display = str(interactor_status.get('display') or 'missing') if isinstance(interactor_status, dict) else 'missing'
    nav['interactor'] = {'text': interactor_display, 'danger': interactor_mode in {'missing', 'none', 'invalid'}}
    validator_status = ctx.get('validator_status') if isinstance(ctx, dict) else None
    validator_mode = str(validator_status.get('mode') or '') if isinstance(validator_status, dict) else ''
    validator_display = str(validator_status.get('display') or 'missing') if isinstance(validator_status, dict) else 'missing'
    nav['validator'] = {'text': validator_display, 'danger': validator_mode in {'missing', 'none', 'invalid'}}
    tests_status = ctx.get('tests_spec_status') if isinstance(ctx, dict) else None
    tests_mode = str(tests_status.get('mode') or '') if isinstance(tests_status, dict) else ''
    tests_total = _to_int(tests_status.get('total') if isinstance(tests_status, dict) else 0)
    tests_sample = _to_int(tests_status.get('sample') if isinstance(tests_status, dict) else 0)
    tests_text = f'{tests_total} ({count_label(tests_sample, "sample")})' if tests_total > 0 else str(tests_status.get('display') or 'empty') if isinstance(tests_status, dict) else 'empty'
    nav['tests'] = {'text': tests_text, 'danger': tests_mode in {'empty', 'invalid', 'missing', 'none'}, 'has_counts': tests_total > 0, 'total': tests_total, 'sample': tests_sample, 'sample_zero': tests_total > 0 and tests_sample <= 0}
    solutions_status = ctx.get('solutions_status') if isinstance(ctx, dict) else None
    solutions_mode = str(solutions_status.get('mode') or '') if isinstance(solutions_status, dict) else ''
    if isinstance(solutions_status, dict) and solutions_mode == 'missing-main':
        count_display = str(solutions_status.get('count_display') or '').strip()
        solutions_text = f'{count_display} (no main correct)' if count_display else 'no main correct'
    else:
        solutions_text = str(solutions_status.get('count_display') or solutions_status.get('display') or 'missing') if isinstance(solutions_status, dict) else 'missing'
    solutions_danger = solutions_mode != 'ready'
    nav['solutions'] = {'text': solutions_text, 'danger': solutions_danger}
    pipeline_blockers: list[str] = []
    if checker_mode in {'missing', 'none'} or checker_display in {'unknown', 'error', 'missing'} or checker_standard_invalid:
        pipeline_blockers.append('checker')
    if validator_mode in {'missing', 'none', 'invalid'}:
        pipeline_blockers.append('validator')
    if tests_mode in {'empty', 'invalid', 'missing', 'none'}:
        pipeline_blockers.append('tests')
    if solutions_danger:
        pipeline_blockers.append('solutions')
    if pipeline_blockers:
        nav['pipeline'] = {'text': 'blocked', 'danger': True}
    else:
        nav['pipeline'] = {'text': 'runnable', 'danger': False}
    verification_status = ctx.get('verification_status') if isinstance(ctx, dict) else None
    verification_mode = str(verification_status.get('mode') or verification_status.get('display') or 'none') if isinstance(verification_status, dict) else 'none'
    verification_display = str(verification_status.get('display') or 'none') if isinstance(verification_status, dict) else 'none'
    nav['run'] = {'text': verification_display, 'danger': verification_mode in {'none', 'failed'}, 'warn': verification_mode == 'stale'}
    workspace_row = ctx.get('workspace')
    problem_row = ctx.get('problem')
    workspace_id = _to_int(_row_value(workspace_row, 'id', 0))
    problem_id = _to_int(_row_value(problem_row, 'id', 0))
    workspace_path_text = str(_row_value(workspace_row, 'path', '') or '').strip()
    workspace_head = str(_row_value(workspace_row, 'head_commit', '') or '').strip()
    workspace_revision = ctx.get('workspace_version')
    head_revision = workspace_revision if isinstance(workspace_revision, int) and workspace_revision > 0 else None
    if head_revision is None and workspace_path_text and workspace_head:
        head_revision = git_commit_count(Path(workspace_path_text), workspace_head)
    export_revision: int | None = None
    if workspace_id > 0 and problem_id > 0 and workspace_path_text:
        latest_export = config.db.fetch_one('\n            SELECT source_commit\n            FROM exports\n            WHERE problem_id=? AND workspace_id=?\n            ORDER BY created_at DESC\n            LIMIT 1\n            ', [problem_id, workspace_id])
        if latest_export is not None:
            export_source_commit = str(_row_value(latest_export, 'source_commit', '') or '').strip()
            if export_source_commit:
                export_revision = git_commit_count(Path(workspace_path_text), export_source_commit)
    if isinstance(export_revision, int) and export_revision > 0:
        export_outdated = isinstance(head_revision, int) and head_revision > 0 and (export_revision != head_revision)
        nav['export'] = {'text': f'built for v{export_revision}', 'danger': bool(export_outdated)}
    else:
        nav['export'] = {'text': 'missing', 'danger': True}
    access_role = str(ctx.get('access', {}).get('role', 'none')) if isinstance(ctx.get('access'), dict) else 'none'
    nav['access'] = {'text': access_role, 'danger': False}
    nav['workspace'] = nav['access']
    return nav

def render_workspace_page(request: Request, problem: str, user: str, *, show_access_admin: bool=False):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    status = config.git_service.status(workspace)
    message = ''
    has_destructive_sudo = has_sudo_session(
        request,
        user_id=int(ctx['user']['id']),
        scope=str(_C.SUDO_SCOPE_DESTRUCTIVE),
    )
    if show_access_admin:
        acl_entries = problem_acl_entries(int(ctx['problem']['id']))
        return template_response(request, 'access.html', {'ctx': ctx, 'message': message, 'acl_entries': acl_entries, 'repo_role_options': ['owner', 'write', 'read']})

    workspace_changes = ctx.get('workspace_changes') if isinstance(ctx, dict) else {}
    change_rows_raw = workspace_changes.get('rows') if isinstance(workspace_changes, dict) else []
    change_rows = [row for row in change_rows_raw if isinstance(row, dict)]
    requested_path = normalize_workspace_rel_path(request.query_params.get('path'))
    selected_path = ''
    if requested_path and any((str(row.get('link_path') or '') == requested_path for row in change_rows)):
        selected_path = requested_path
    elif change_rows:
        selected_path = str(change_rows[0].get('link_path') or '')

    selected_diff = ''
    selected_diff_truncated = False
    selected_diff_lines: list[dict[str, str]] = []
    if selected_path:
        try:
            selected_diff, selected_diff_truncated = config.git_service.diff_for_path(workspace, selected_path)
        except (ValueError, RuntimeError):
            selected_diff = ''
            selected_diff_truncated = False
    if selected_diff:
        for raw in str(selected_diff).splitlines():
            line = str(raw)
            kind = 'ctx'
            if line.startswith('diff --git ') or line.startswith('index ') or line.startswith('new file mode ') or line.startswith('deleted file mode ') or line.startswith('--- ') or line.startswith('+++ '):
                continue
            elif line.startswith('@@'):
                kind = 'hunk'
            elif line.startswith('+'):
                kind = 'add'
            elif line.startswith('-'):
                kind = 'del'
            selected_diff_lines.append({'text': line, 'kind': kind})
    return template_response(request, 'workspace.html', {'ctx': ctx, 'status': status, 'branches': ctx.get('branches', []), 'message': message, 'selected_path': selected_path, 'selected_diff': selected_diff, 'selected_diff_truncated': bool(selected_diff_truncated), 'selected_diff_lines': selected_diff_lines, 'change_rows': change_rows, 'has_destructive_sudo': bool(has_destructive_sudo)})




