from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from app.db import now_iso
from app.impl.auth import _parse_iso_utc, _template_response, _utc_now
from app.impl.config import config
from app.main_utils import (
    _compact_error_text,
    _contains_symlink_component,
    _normalize_optional_component_source_path_safe,
    _normalize_workspace_rel_path,
    _safe_workspace_path,
    _sanitize_log_text_for_ui,
    upload_compile_check_error as _upload_compile_check_error_impl,
    workspace_source_compile_check_error as _workspace_source_compile_check_error_impl,
)
from app.services.solution_metadata import (
    EXPECTED_BEHAVIOR_VALUES,
    desc_rel_path_for_source,
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
    parse_solution_desc,
    render_solution_desc,
)
from app.services.statement_template import statement_sources_signature
from app.services.tests_spec import (
    TESTS_SPEC_REL,
    dumps_tests_spec,
    load_tests_spec,
    normalize_gen_command,
    normalize_manual_input,
    normalize_test_id,
    normalize_test_kind,
    parse_gen_command_tokens,
    payload_rel_path_for_test,
    summarize_tests_spec,
)
from app.services.util import is_canonical_artifact_id, run_cmd

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
        access = _workspace_access_context(int(problem_row['id']), int(user_row['id']))
        _require_read_access({'access': access})
        if refresh_status:
            config.workspace_service.ensure_workspace(problem, user, refresh_status=True)
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
    workspace_branch = str(ctx['workspace'].get('branch') or 'main').strip() or 'main'
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    try:
        _payload, general_cfg, _cfg_path = _read_problem_config(workspace_path)
        safe_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        ctx['problem_mode'] = safe_mode
        ctx['general_cfg'] = {'time_limit_ms': _coerce_int(general_cfg.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS), 'memory_limit_mb': _coerce_int(general_cfg.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB), 'mode': safe_mode}
    except Exception:
        ctx['problem_mode'] = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
        ctx['general_cfg'] = {'time_limit_ms': int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), 'memory_limit_mb': int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), 'mode': str(_C.GENERAL_CONFIG_DEFAULTS['mode'])}
    ctx['workspace_revision'] = _workspace_revision_info(workspace_path, workspace_branch)
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
        ctx['checker_status'] = _checker_status_context(workspace_path)
    except Exception:
        ctx['checker_status'] = {'mode': 'missing', 'display': 'unknown', 'standard_checker': '', 'standard_valid': False, 'repo_source': 'checkers/checker.cpp', 'repo_source_exists': False}
    try:
        ctx['generator_status'] = _generator_status_context(workspace_path)
    except Exception:
        ctx['generator_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'generators/generator.cpp', 'repo_source_exists': False, 'configured_sources': [], 'source_rows_truncated': False}
    try:
        ctx['interactor_status'] = _interactor_status_context(workspace_path)
    except Exception:
        ctx['interactor_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'interactors/interactor.cpp', 'repo_source_exists': False}
    try:
        ctx['validator_status'] = _validator_status_context(workspace_path)
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
            'build_id': '',
            'error': '',
            'created_at': '',
            'stale': False,
            'stale_reason': '',
        }
    latest_build = ctx.get('latest_build')
    latest_preview = ctx.get('latest_preview')
    ctx['latest_build_version'] = _artifact_version_number(latest_build['id']) if latest_build else None
    ctx['latest_preview_version'] = _artifact_version_number(latest_preview['id']) if latest_preview else None
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
    mode_text = _normalize_problem_mode(general_cfg.get('mode') if isinstance(general_cfg, dict) else str(_C.GENERAL_CONFIG_DEFAULTS['mode']), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
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
                _safe_artifact_path(problem_slug, preview_id, 'statement_preview/statement.pdf')
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
                summary_obj = _parse_summary_json(_row_value(preview_row, 'summary_json', None), f'preview/{preview_id}') or {}
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
        generator_text = f'{configured_count} files, {used_count} used'
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
    tests_text = f'{tests_total} ({tests_sample} sample)' if tests_total > 0 else str(tests_status.get('display') or 'empty') if isinstance(tests_status, dict) else 'empty'
    nav['tests'] = {'text': tests_text, 'danger': tests_mode in {'empty', 'invalid', 'missing', 'none'}, 'has_counts': tests_total > 0, 'total': tests_total, 'sample': tests_sample, 'sample_zero': tests_total > 0 and tests_sample <= 0}
    solutions_status = ctx.get('solutions_status') if isinstance(ctx, dict) else None
    solutions_mode = str(solutions_status.get('mode') or '') if isinstance(solutions_status, dict) else ''
    if isinstance(solutions_status, dict) and solutions_mode == 'missing-main':
        count_display = str(solutions_status.get('count_display') or '').strip()
        solutions_text = f'{count_display} (no main correct)' if count_display else 'no main correct'
    else:
        solutions_text = str(solutions_status.get('count_display') or solutions_status.get('display') or 'missing') if isinstance(solutions_status, dict) else 'missing'
    nav['solutions'] = {'text': solutions_text, 'danger': solutions_mode != 'ready'}
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
        head_revision = _git_commit_count(Path(workspace_path_text), workspace_head)
    export_revision: int | None = None
    if workspace_id > 0 and problem_id > 0 and workspace_path_text:
        latest_export = config.db.fetch_one('\n            SELECT source_commit\n            FROM exports\n            WHERE problem_id=? AND workspace_id=?\n            ORDER BY created_at DESC\n            LIMIT 1\n            ', [problem_id, workspace_id])
        if latest_export is not None:
            export_source_commit = str(_row_value(latest_export, 'source_commit', '') or '').strip()
            if export_source_commit:
                export_revision = _git_commit_count(Path(workspace_path_text), export_source_commit)
    if isinstance(export_revision, int) and export_revision > 0:
        export_outdated = isinstance(head_revision, int) and head_revision > 0 and (export_revision != head_revision)
        nav['export'] = {'text': f'built for v{export_revision}', 'danger': bool(export_outdated)}
    else:
        nav['export'] = {'text': 'missing', 'danger': True}
    access_role = str(ctx.get('access', {}).get('role', 'none')) if isinstance(ctx.get('access'), dict) else 'none'
    nav['access'] = {'text': access_role, 'danger': False}
    nav['workspace'] = nav['access']
    return nav

def _user_participating_problems(user_id: int, limit: int=_C.API_PROBLEMS_LIST_LIMIT) -> list[dict]:
    uid = int(user_id)
    cap = max(1, int(limit))
    rows = config.db.fetch_all('\n        SELECT p.slug,p.name,a.role AS role,\n               w.id AS workspace_id,w.path,w.branch,w.head_commit,w.dirty,w.updated_at\n        FROM repo_acl a\n        JOIN problems p ON p.id=a.problem_id\n        LEFT JOIN workspaces w ON w.problem_id=p.id AND w.user_id=?\n        WHERE a.user_id=?\n        ORDER BY p.slug ASC\n        LIMIT ?\n        ', [uid, uid, cap])
    items: list[dict] = []
    for row in rows:
        role = str(row['role'] or 'read').strip().lower()
        if role not in {'owner', 'write', 'read'}:
            role = 'read'
        head = str(row['head_commit'] or '').strip()
        branch = str(row['branch'] or 'main').strip() or 'main'
        dirty_raw = row['dirty']
        dirty = False
        if dirty_raw is not None:
            try:
                dirty = int(dirty_raw) != 0
            except Exception:
                dirty = bool(dirty_raw)
        workspace_path_raw = str(row['path'] or '').strip()
        if workspace_path_raw:
            revision = _workspace_revision_info(Path(workspace_path_raw), branch, fetch_remote=False)
        else:
            revision = {'local': None, 'upstream': None, 'display': 'none / upstream missing', 'highlight': True, 'upstream_higher': False, 'missing': True}
        items.append({'slug': str(row['slug']), 'name': str(row['name']), 'role': role, 'workspace_id': row['workspace_id'], 'has_workspace': row['workspace_id'] is not None, 'workspace_path': workspace_path_raw, 'branch': branch, 'head_commit': head, 'head_short': head[:8], 'dirty': dirty, 'revision_local': revision['local'], 'revision_upstream': revision['upstream'], 'revision_display': revision['display'], 'revision_highlight': revision['highlight'], 'revision_upstream_higher': revision['upstream_higher'], 'revision_missing': revision['missing'], 'updated_at': row['updated_at']})
    return items

def _default_problem_slug_for_user(username: str) -> str:
    safe_user = str(username or '').strip()
    if not _C.USER_IDENT_RE.fullmatch(safe_user):
        return ''
    row = config.db.fetch_one('SELECT id FROM users WHERE username=?', [safe_user])
    if row is None:
        return ''
    items = _user_participating_problems(int(row['id']), limit=1)
    if items:
        return str(items[0]['slug'])
    return ''

def _global_user_ctx(username: str) -> dict:
    safe_user = str(username or '').strip()
    if not _C.USER_IDENT_RE.fullmatch(safe_user):
        raise HTTPException(status_code=400, detail=_C.USERNAME_RULE_MESSAGE)
    row = config.db.fetch_one('SELECT id,username FROM users WHERE username=?', [safe_user])
    if row is None:
        try:
            ensured = config.workspace_service.ensure_user(safe_user)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        row = {'id': ensured['id'], 'username': ensured['username']}
    return {'user': {'id': int(row['id']), 'username': str(row['username'])}, 'default_problem': _default_problem_slug_for_user(safe_user)}

def _normalize_contest_role(raw: object) -> str:
    role = str(raw or '').strip().lower()
    if role in {'owner', 'write', 'read'}:
        return role
    return 'read'

def _normalize_contest_slug_required(value: str) -> str:
    slug = str(value or '').strip()
    if not _C.CONTEST_IDENT_RE.fullmatch(slug):
        raise ValueError('invalid contest slug')
    return slug

def _normalize_contest_title_required(value: str) -> str:
    title = str(value or '').strip()
    if not title:
        raise ValueError('contest title is required')
    if len(title) > _C.CONTEST_TITLE_MAX_LEN:
        raise ValueError(f'contest title is too long (max {_C.CONTEST_TITLE_MAX_LEN})')
    return title

def _normalize_problem_name_required(value: str) -> str:
    name = str(value or '').strip()
    if not name:
        raise ValueError('problem name is required')
    if len(name) > _C.PROBLEM_NAME_MAX_LEN:
        raise ValueError(f'problem name is too long (max {_C.PROBLEM_NAME_MAX_LEN})')
    return name

def _user_contests_overview(user_id: int, limit: int=_C.API_PROBLEMS_LIST_LIMIT) -> list[dict]:
    uid = int(user_id)
    cap = max(1, int(limit))
    rows = config.db.fetch_all("\n        SELECT c.id,c.slug,c.title,c.owner_user_id,c.created_at,m.role,\n               (\n                   SELECT COUNT(*)\n                   FROM contest_problems cp\n                   WHERE cp.contest_id=c.id\n               ) AS problem_count,\n               (\n                   SELECT group_concat(x.slug, ', ')\n                   FROM (\n                       SELECT p.slug AS slug\n                       FROM contest_problems cp\n                       JOIN problems p ON p.id=cp.problem_id\n                       WHERE cp.contest_id=c.id\n                       ORDER BY p.slug ASC\n                       LIMIT 5\n                   ) x\n               ) AS problem_slugs_preview,\n               (\n                   SELECT COUNT(*)\n                   FROM contest_problems cp3\n                   JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?\n                   WHERE cp3.contest_id=c.id\n                     AND COALESCE(w.dirty, 0) <> 0\n               ) AS dirty_problem_count\n        FROM contests c\n        JOIN contest_members m ON m.contest_id=c.id\n        WHERE m.user_id=?\n        ORDER BY c.slug ASC\n        LIMIT ?\n        ", [uid, uid, cap])
    entries: list[dict] = []
    for row in rows:
        try:
            problem_count = max(0, int(row['problem_count'] or 0))
        except Exception:
            problem_count = 0
        try:
            dirty_problem_count = max(0, int(row['dirty_problem_count'] or 0))
        except Exception:
            dirty_problem_count = 0
        preview = str(row['problem_slugs_preview'] or '').strip()
        entries.append({'id': int(row['id']), 'slug': str(row['slug']), 'title': str(row['title']), 'owner_user_id': int(row['owner_user_id']), 'created_at': row['created_at'], 'role': _normalize_contest_role(row['role']), 'problem_count': problem_count, 'problem_slugs_preview': preview, 'problem_preview_truncated': problem_count > 5, 'dirty_problem_count': dirty_problem_count, 'has_dirty': dirty_problem_count > 0})
    return entries

def _workspace_access_context(problem_id: int, user_id: int) -> dict:
    row = config.db.fetch_one('SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?', [problem_id, user_id])
    if row is None:
        return {'role': 'none', 'can_read': False, 'can_write': False, 'can_manage': False, 'read_block_reason': 'you do not have access to this problem', 'write_block_reason': 'write access required', 'manage_block_reason': 'owner access required'}
    role = str(row['role']).strip().lower()
    if role not in {'owner', 'write', 'read'}:
        role = 'read'
    can_write = role in {'owner', 'write'}
    return {'role': role, 'can_read': True, 'can_write': can_write, 'can_manage': role == 'owner', 'read_block_reason': '', 'write_block_reason': '' if can_write else 'read-only access', 'manage_block_reason': '' if role == 'owner' else 'owner access required'}

def _normalize_repo_role(raw: object) -> str:
    role = str(raw or '').strip().lower()
    if role in {'owner', 'write', 'read'}:
        return role
    raise ValueError('invalid role')

def _problem_owner_count(problem_id: int) -> int:
    row = config.db.fetch_one("SELECT COUNT(*) AS c FROM repo_acl WHERE problem_id=? AND role='owner'", [int(problem_id)])
    if row is None:
        return 0
    try:
        return max(0, int(row['c'] or 0))
    except Exception:
        return 0

def _problem_acl_entries(problem_id: int) -> list[dict]:
    rows = config.db.fetch_all("\n        SELECT u.username,a.role,a.created_at\n        FROM repo_acl a\n        JOIN users u ON u.id=a.user_id\n        WHERE a.problem_id=?\n        ORDER BY\n            CASE a.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,\n            u.username ASC\n        ", [int(problem_id)])
    entries: list[dict] = []
    for row in rows:
        role = str(row['role'] or '').strip().lower()
        if role not in {'owner', 'write', 'read'}:
            role = 'read'
        entries.append({'username': str(row['username']), 'role': role, 'created_at': row['created_at']})
    return entries

def _require_read_access(ctx: dict) -> None:
    access = ctx.get('access') if isinstance(ctx, dict) else None
    can_read = bool(access.get('can_read')) if isinstance(access, dict) else False
    if can_read:
        return
    reason = 'problem access required'
    if isinstance(access, dict):
        reason = str(access.get('read_block_reason') or reason)
    raise HTTPException(status_code=403, detail=reason)

def _require_write_access(ctx: dict) -> None:
    access = ctx.get('access') if isinstance(ctx, dict) else None
    can_write = bool(access.get('can_write')) if isinstance(access, dict) else False
    if can_write:
        return
    reason = 'write access required'
    if isinstance(access, dict):
        reason = str(access.get('write_block_reason') or reason)
    raise HTTPException(status_code=403, detail=reason)

def _require_manage_access(ctx: dict) -> None:
    access = ctx.get('access') if isinstance(ctx, dict) else None
    can_manage = bool(access.get('can_manage')) if isinstance(access, dict) else False
    if can_manage:
        return
    reason = 'owner access required'
    if isinstance(access, dict):
        reason = str(access.get('manage_block_reason') or reason)
    raise HTTPException(status_code=403, detail=reason)

def _is_system_admin_user_id(user_id: int) -> bool:
    uid = int(user_id)
    if uid <= 0:
        return False
    row = config.db.fetch_one('SELECT is_system_admin FROM users WHERE id=?', [uid])
    if row is None:
        return False
    try:
        return int(row['is_system_admin'] or 0) == 1
    except Exception:
        return False

def _require_system_admin(ctx: dict) -> None:
    user_row = ctx.get('user') if isinstance(ctx, dict) else None
    user_id = 0
    if isinstance(user_row, dict):
        try:
            user_id = int(user_row.get('id') or 0)
        except Exception:
            user_id = 0
    if _is_system_admin_user_id(user_id):
        if isinstance(user_row, dict):
            user_row['is_system_admin'] = 1
        return
    raise HTTPException(status_code=403, detail='system admin required')

def _cleanup_runtime_cache(force: bool=False) -> None:
    try:
        config.runtime_cache_service.cleanup_cache(force=force)
    except Exception:
        pass

def _git_commit_count(workspace: Path, rev: str) -> int | None:
    try:
        proc = run_cmd(['git', '-C', str(workspace), 'rev-list', '--count', str(rev)])
        if proc.returncode != 0:
            return None
        value = int(str(proc.stdout or '').strip())
        return value if value >= 0 else None
    except Exception:
        return None

def _git_commit_sha(workspace: Path, rev: str) -> str | None:
    try:
        proc = run_cmd(['git', '-C', str(workspace), 'rev-parse', '--verify', str(rev)])
        if proc.returncode != 0:
            return None
        value = str(proc.stdout or '').strip()
        return value or None
    except Exception:
        return None

def _workspace_origin_local_repo(workspace: Path) -> Path | None:
    try:
        proc = run_cmd(['git', '-C', str(workspace), 'remote', 'get-url', 'origin'], timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = str(proc.stdout or '').strip()
    if not raw:
        return None
    remote_path: Path | None = None
    if raw.startswith('file://'):
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in ('', 'localhost'):
            return None
        decoded = unquote(parsed.path or '')
        if not decoded:
            return None
        remote_path = Path(decoded)
    elif '://' in raw:
        return None
    elif ':' in raw and (not raw.startswith('/')) and (not raw.startswith('./')) and (not raw.startswith('../')):
        return None
    else:
        remote_path = Path(raw)
    if not remote_path.is_absolute():
        remote_path = (workspace / remote_path).resolve()
    else:
        remote_path = remote_path.resolve()
    return remote_path if remote_path.exists() else None

def _workspace_upstream_revision_info(workspace: Path, branch: str) -> tuple[int | None, str | None]:
    upstream_ref = f'origin/{branch}'
    origin_repo = _workspace_origin_local_repo(workspace)
    if origin_repo is not None:
        upstream_branch_ref = f'refs/heads/{branch}'
        version = _git_commit_count(origin_repo, upstream_branch_ref)
        commit = _git_commit_sha(origin_repo, upstream_branch_ref)
        if version is not None or commit is not None:
            return (version, commit)
    return (_git_commit_count(workspace, upstream_ref), _git_commit_sha(workspace, upstream_ref))

def _workspace_revision_info(workspace: Path, branch: str='main', *, fetch_remote: bool=False) -> dict:
    safe_branch = str(branch or 'main').strip() or 'main'
    if any((ch.isspace() for ch in safe_branch)):
        safe_branch = 'main'
    _ = fetch_remote
    upstream_ref = f'origin/{safe_branch}'
    local_version = _git_commit_count(workspace, 'HEAD')
    local_commit = _git_commit_sha(workspace, 'HEAD')
    upstream_version, upstream_commit = _workspace_upstream_revision_info(workspace, safe_branch)
    ahead_count: int | None = None
    behind_count: int | None = None
    if _git_commit_sha(workspace, upstream_ref) is not None:
        try:
            proc = run_cmd(['git', '-C', str(workspace), 'rev-list', '--left-right', '--count', f'HEAD...{upstream_ref}'], timeout=30)
            if proc.returncode == 0:
                parts = str(proc.stdout or '').strip().split()
                if len(parts) >= 2:
                    ahead_count = max(0, int(parts[0]))
                    behind_count = max(0, int(parts[1]))
        except Exception:
            ahead_count = None
            behind_count = None
    elif local_commit is not None and upstream_commit is not None and (local_commit == upstream_commit):
        ahead_count = 0
        behind_count = 0
    upstream_higher = False
    if local_version is not None and upstream_version is not None:
        upstream_higher = upstream_version > local_version
    elif behind_count is not None:
        upstream_higher = behind_count > 0
    missing = local_version is None or upstream_version is None
    local_text = f'v{local_version}' if local_version is not None else 'none'
    upstream_text = f'v{upstream_version}' if upstream_version is not None else 'missing'
    display = f'{local_text} / upstream {upstream_text}'
    highlight = bool(upstream_higher or missing)
    return {'local': local_version, 'upstream': upstream_version, 'display': display, 'highlight': highlight, 'upstream_higher': bool(upstream_higher), 'missing': bool(missing), 'ahead_count': ahead_count, 'behind_count': behind_count}

def _artifact_version_number(artifact_id: str | None) -> int | None:
    raw = str(artifact_id or '').strip()
    if not raw:
        return None
    tail = raw.rsplit('-', 1)[-1]
    if tail.isdigit():
        try:
            return int(tail)
        except Exception:
            return None
    return None

def _artifact_root(problem: str, artifact_id: str) -> Path:
    aid = str(artifact_id or '')
    if not is_canonical_artifact_id(aid):
        raise HTTPException(status_code=404, detail='artifact not found')
    base = (config.settings.artifacts_root / problem).resolve()
    root = (base / aid).resolve()
    try:
        rel = root.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail='artifact not found')
    if len(rel.parts) != 1 or rel.parts[0] != aid:
        raise HTTPException(status_code=404, detail='artifact not found')
    return root

def _safe_artifact_path(problem: str, build_id: str, rel: str) -> Path:
    root = _artifact_root(problem, build_id)
    candidate = root / rel
    path = candidate.resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail='invalid artifact path')
    if _contains_symlink_component(root, candidate):
        raise HTTPException(status_code=404, detail='artifact file not found')
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail='artifact file not found')
    return path

def _iter_safe_descendant_files(root: Path, target: Path):
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        dir_root = Path(dirpath)
        pruned_dirs: list[str] = []
        for name in dirnames:
            d = dir_root / name
            if d.is_symlink():
                continue
            try:
                resolved = d.resolve()
            except OSError:
                continue
            if root_resolved in resolved.parents or root_resolved == resolved:
                pruned_dirs.append(name)
        dirnames[:] = sorted(pruned_dirs)
        safe_filenames: list[str] = []
        for name in filenames:
            p = dir_root / name
            if p.is_symlink():
                continue
            try:
                resolved = p.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents and root_resolved != resolved:
                continue
            safe_filenames.append(name)
        for name in sorted(safe_filenames):
            yield (dir_root / name)

def _browser_file_response(file_path: Path) -> FileResponse:
    headers = {'X-Content-Type-Options': 'nosniff'}
    if file_path.suffix.lower() == '.pdf':
        return FileResponse(file_path, filename=file_path.name, media_type='application/pdf', content_disposition_type='inline', headers=headers)
    text_like_suffixes = {'.log', '.txt', '.tex', '.json', '.md', '.csv', '.xml', '.yaml', '.yml', '.in', '.out', '.ans'}
    suffix = file_path.suffix.lower()
    if suffix in text_like_suffixes:
        return FileResponse(file_path, filename=file_path.name, media_type='text/plain; charset=utf-8', headers=headers)
    return FileResponse(file_path, filename=file_path.name, headers=headers)

def _export_download_filename(ctx: dict, build_id: str, stored_filename: str) -> str | None:
    safe_build_id = str(build_id or '').strip()
    safe_filename = Path(str(stored_filename or '')).name.strip()
    if not safe_build_id or not safe_filename:
        return None
    row = config.db.fetch_one('\n        SELECT source_commit\n        FROM exports\n        WHERE problem_id=? AND workspace_id=? AND build_id=? AND filename=?\n        ORDER BY created_at DESC\n        LIMIT 1\n        ', [ctx['problem']['id'], ctx['workspace']['id'], safe_build_id, safe_filename])
    if row is None:
        return None
    source_commit = str(row['source_commit'] or '').strip()
    revision = _git_commit_count(Path(ctx['workspace']['path']), source_commit) if source_commit else None
    revision_display = f'v{revision}' if isinstance(revision, int) and revision >= 0 else 'v?'
    problem_slug = str(ctx['problem'].get('slug') or '').strip()
    if not problem_slug:
        return None
    return f'{problem_slug}-{revision_display}.zip'

def _workspace_run_artifact_root(ctx: dict, run_id: str) -> Path:
    row = config.db.fetch_one('SELECT artifact_path FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [run_id, ctx['problem']['id'], ctx['workspace']['id']])
    if row is None:
        raise HTTPException(status_code=404, detail='run not found in workspace')
    root = Path(str(row['artifact_path'] or '')).resolve()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail='run artifact directory not found')
    valid = False
    artifacts_problem_root = (config.settings.artifacts_root / ctx['problem']['slug']).resolve()
    invalid_runs_root = (config.settings.run_root / 'invalid-runs').resolve()
    try:
        rel = root.relative_to(artifacts_problem_root)
        if len(rel.parts) == 3 and rel.parts[1] == 'logs' and (rel.parts[2] == f'run-{run_id}') and is_canonical_artifact_id(rel.parts[0]):
            valid = True
    except ValueError:
        pass
    if root == (invalid_runs_root / run_id).resolve():
        valid = True
    if not valid:
        raise HTTPException(status_code=404, detail='run artifact directory not found')
    return root

def _normalize_run_artifact_rel(rel: str) -> str:
    return rel.lstrip('/')

def _safe_run_artifact_path(ctx: dict, run_id: str, rel: str) -> Path:
    root = _workspace_run_artifact_root(ctx, run_id)
    norm_rel = _normalize_run_artifact_rel(rel)
    candidate = root / norm_rel
    path = candidate.resolve()
    if root not in path.parents and root != path:
        raise HTTPException(status_code=400, detail='invalid run artifact path')
    if _contains_symlink_component(root, candidate):
        raise HTTPException(status_code=404, detail='run artifact file not found')
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail='run artifact file not found')
    return path

def _assert_workspace_build_access(ctx: dict, build_id: str) -> None:
    row = config.db.fetch_one('SELECT id FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [build_id, ctx['problem']['id'], ctx['workspace']['id']])
    if row is None:
        raise HTTPException(status_code=404, detail='build not found in workspace')

def _assert_workspace_artifact_access(ctx: dict, artifact_id: str) -> None:
    aid = str(artifact_id or '').strip()
    row = None
    if aid.startswith('b-'):
        row = config.db.fetch_one('SELECT id FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [aid, ctx['problem']['id'], ctx['workspace']['id']])
    elif aid.startswith('p-'):
        row = config.db.fetch_one('SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [aid, ctx['problem']['id'], ctx['workspace']['id']])
    else:
        row = config.db.fetch_one('\n            SELECT id FROM (\n                SELECT id\n                FROM builds\n                WHERE id=? AND problem_id=? AND workspace_id=?\n                UNION ALL\n                SELECT id\n                FROM previews\n                WHERE id=? AND problem_id=? AND workspace_id=?\n            )\n            LIMIT 1\n            ', [aid, ctx['problem']['id'], ctx['workspace']['id'], aid, ctx['problem']['id'], ctx['workspace']['id']])
    if row is not None:
        return
    raise HTTPException(status_code=404, detail='artifact not found in workspace')

def _audit(actor_user_id: int, problem_id: int | None, action: str, details: dict) -> None:
    config.db.execute('INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at) VALUES(?,?,?,?,?)', [actor_user_id, problem_id, action, json.dumps(details), now_iso()])

def _normalize_page_target(page: str) -> str:
    raw = str(page or '').strip().lower()
    if raw == 'build':
        raw = 'tests'
    allowed = {
        'problems',
        'general',
        'contests',
        'files',
        'generators',
        'checker',
        'interactor',
        'validator',
        'solutions',
        'git',
        'workspace',
        'tests',
        'preview',
        'run',
        'export',
        'access',
        'history',
        'settings',
    }
    return raw if raw in allowed else 'tests'

def _parse_summary_json(raw: str | None, label: str) -> dict | None:
    if not raw:
        return None
    text = str(raw)
    if len(text) > _C.SUMMARY_JSON_UI_CHAR_LIMIT:
        return {'error': f'summary_json for {label} exceeds UI parse limit ({_C.SUMMARY_JSON_UI_CHAR_LIMIT} chars)', 'summary_json_too_large': True, 'summary_json_chars': len(text), 'summary_json_char_limit': _C.SUMMARY_JSON_UI_CHAR_LIMIT}
    try:
        payload = json.loads(text)
    except Exception:
        return {'error': f'invalid summary_json for {label}'}
    if isinstance(payload, dict):
        return payload
    return {'error': f'summary_json for {label} must be a JSON object'}

def _read_text_safe_limited(path: Path, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    with path.open('r', encoding='utf-8', errors='replace') as fh:
        text = fh.read(cap + 1)
    if len(text) <= cap:
        return (text, False)
    clipped = text[:cap]
    if '\n' in clipped:
        clipped = clipped.rsplit('\n', 1)[0] + '\n'
    clipped += f'... [truncated; showing first {cap} characters]\n'
    return (clipped, True)

def _read_workspace_source_with_default(workspace: Path, rel: Path, default_text: str) -> tuple[str, bool]:
    try:
        file_path = _safe_workspace_path(workspace, rel.as_posix())
    except HTTPException:
        return (default_text, False)
    if not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        return (default_text, False)
    return _read_text_safe_limited(file_path, _C.STATEMENT_EDITOR_CHAR_LIMIT)

def _parse_line_param(raw: str | None, default: int=1) -> int:
    try:
        line = int(str(raw or '').strip())
    except Exception:
        return default
    return line if line > 0 else default

def _build_line_focus_context(text: str, line: int, radius: int=2) -> dict | None:
    rows = str(text or '').splitlines()
    if not rows:
        return None
    target = int(line)
    if target <= 0 or target > len(rows):
        return None
    start = max(1, target - max(0, int(radius)))
    end = min(len(rows), target + max(0, int(radius)))
    snippet: list[str] = []
    for ln in range(start, end + 1):
        marker = '>' if ln == target else ' '
        snippet.append(f'{marker} {ln:5d} | {rows[ln - 1]}')
    return {'start': start, 'end': end, 'line': target, 'text': '\n'.join(snippet)}

def _normalize_files_source(raw: str | None) -> str:
    value = str(raw or '').strip().lower()
    if value == 'build':
        value = 'tests'
    allowed = {'tests', 'preview', 'run', 'export', 'workspace', 'access', 'general', 'checker', 'validator', 'interactor', 'solutions', 'generators'}
    return value if value in allowed else ''

def _normalize_source_id(raw: str | None) -> str:
    value = str(raw or '').strip()
    return value if is_canonical_artifact_id(value) else ''

def _files_back_target(problem: str, user: str, source: str, source_id: str) -> tuple[str, str]:
    base = f'/problems/{problem}/{user}'
    if source in {'build', 'tests'}:
        if source_id:
            return (f'{base}/tests?build_id={quote_plus(source_id)}', 'Tests')
        return (f'{base}/tests', 'Tests')
    if source == 'preview':
        if source_id:
            return (f'{base}/preview?preview_id={quote_plus(source_id)}', 'Statement')
        return (f'{base}/preview', 'Statement')
    if source == 'run':
        if source_id:
            return (f'{base}/run/details?run_id={quote_plus(source_id)}', 'Invocations')
        return (f'{base}/run', 'Invocations')
    if source == 'export':
        return (f'{base}/export', 'Packages')
    if source == 'workspace':
        return (f'{base}/workspace', 'Working copy')
    if source == 'access':
        return (f'{base}/access', 'Manage access')
    if source == 'general':
        return (f'{base}/general', 'General info')
    if source == 'checker':
        return (f'{base}/checker', 'Checker')
    if source == 'validator':
        return (f'{base}/validator', 'Validator')
    if source == 'interactor':
        return (f'{base}/interactor', 'Interactor')
    if source == 'solutions':
        return (f'{base}/solutions', 'Solution files')
    if source == 'generators':
        return (f'{base}/generators', 'Generators')
    return ('', '')

def _files_source_query_tail(source: str, source_id: str) -> str:
    parts: list[str] = []
    if source:
        parts.append(f'src={quote_plus(source)}')
    if source_id:
        parts.append(f'sid={quote_plus(source_id)}')
    if not parts:
        return ''
    return '&' + '&'.join(parts)

def _normalize_repo_dir(raw: str | None) -> str:
    text = str(raw or '').strip().replace('\\', '/')
    if not text:
        return ''
    if text.startswith('/'):
        return ''
    parts: list[str] = []
    for part in text.split('/'):
        item = part.strip()
        if not item or item == '.':
            continue
        if item == '..':
            return ''
        parts.append(item)
    return '/'.join(parts)

def _files_browse_query_tail(repo_dir: str) -> str:
    parts: list[str] = []
    dir_norm = _normalize_repo_dir(repo_dir)
    if dir_norm:
        parts.append(f'dir={quote_plus(dir_norm)}')
    if not parts:
        return ''
    return '&' + '&'.join(parts)

def _build_repo_browser_entries(workspace: Path, paths: list[str], browse_dir: str) -> tuple[str, str, list[dict[str, str]], list[dict[str, str]], int]:
    normalized: list[dict[str, object]] = []
    for raw in paths:
        rel = str(raw).strip()
        if not rel:
            continue
        is_dir = False
        try:
            abs_path = _safe_workspace_path(workspace, rel)
            is_dir = abs_path.exists() and abs_path.is_dir()
        except HTTPException:
            is_dir = False
        normalized.append({'path': rel, 'is_dir': is_dir})
    dir_norm = _normalize_repo_dir(browse_dir)
    prefix = f'{dir_norm}/' if dir_norm else ''
    if dir_norm and (not any((str(row['path']) == dir_norm or str(row['path']).startswith(prefix) for row in normalized))):
        dir_norm = ''
        prefix = ''
    child_dirs: dict[str, str] = {}
    child_files: list[dict[str, str]] = []
    for row in normalized:
        full = str(row['path'])
        if full == dir_norm:
            continue
        if prefix and (not full.startswith(prefix)):
            continue
        rel = full[len(prefix):] if prefix else full
        if not rel:
            continue
        if '/' in rel:
            name = rel.split('/', 1)[0]
            child_dirs.setdefault(name, f'{prefix}{name}' if prefix else name)
        elif bool(row['is_dir']):
            child_dirs.setdefault(rel, f'{prefix}{rel}' if prefix else rel)
        else:
            child_files.append({'name': rel, 'path': f'{prefix}{rel}' if prefix else rel})
    dirs = [{'name': name, 'path': child_dirs[name]} for name in sorted(child_dirs)]
    files = sorted(child_files, key=lambda row: row['name'])
    parent = dir_norm.rsplit('/', 1)[0] if '/' in dir_norm else ''
    return (dir_norm, parent, dirs, files, len(normalized))

def _kind_for_path(path: str) -> str:
    target = str(path or '').strip()
    for row in _C.CORE_SOURCE_TARGETS:
        if str(row['path']) == target:
            return str(row['kind'])
    return ''

def _default_files_selected_path(workspace: Path, listed_paths: list[str]) -> str:
    for rel in ['config/problem.json', 'statement/content.tex', 'config/build.json', 'solutions/accepted.cpp']:
        try:
            candidate = _safe_workspace_path(workspace, rel)
        except HTTPException:
            continue
        if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
            return rel
    for rel in listed_paths:
        safe_rel = _normalize_workspace_rel_path(rel)
        if not safe_rel:
            continue
        try:
            candidate = _safe_workspace_path(workspace, safe_rel)
        except HTTPException:
            continue
        if candidate.exists() and candidate.is_file() and (not candidate.is_symlink()):
            return safe_rel
    return 'config/problem.json'

def _template_for_kind(kind: str) -> str:
    key = str(kind or '').strip().lower()
    if key not in _C.FILE_TEMPLATES:
        raise ValueError('unknown template kind')
    return str(_C.FILE_TEMPLATES[key])

def _standard_checker_options() -> list[str]:
    root = _C.STANDARD_CHECKER_ROOT
    try:
        if not root.exists() or not root.is_dir() or root.is_symlink():
            return []
    except OSError:
        return []
    names: list[str] = []
    try:
        for item in sorted(root.iterdir(), key=lambda path: path.name):
            name = str(item.name or '')
            if item.is_symlink() or not item.is_file():
                continue
            if Path(name).suffix.lower() != '.cpp':
                continue
            if not _C.STANDARD_CHECKER_NAME_RE.fullmatch(name):
                continue
            names.append(name)
    except OSError:
        return []
    return names

def _standard_checker_catalog() -> list[dict]:
    catalog: list[dict] = []
    for name in _standard_checker_options():
        canonical = f'std::{name}'
        description = str(_C.STANDARD_CHECKER_DESCRIPTIONS.get(name, 'general-purpose standard checker from testlib'))
        catalog.append({'name': name, 'value': canonical, 'description': description, 'label': f'{canonical} - {description}'})
    return catalog

def _normalize_standard_checker_name(raw: str) -> str:
    value = str(raw or '').strip()
    if value.startswith('std::'):
        value = value[5:]
    if not value:
        raise ValueError('standard checker name is required')
    if '/' in value or '\\' in value:
        raise ValueError('invalid standard checker name')
    if not value.endswith('.cpp'):
        value += '.cpp'
    if not _C.STANDARD_CHECKER_NAME_RE.fullmatch(value):
        raise ValueError('invalid standard checker name')
    return value

def _canonical_standard_checker_name(raw: str) -> str:
    return f'std::{_normalize_standard_checker_name(raw)}'

def _resolve_standard_checker_path(raw_name: str) -> tuple[str, Path]:
    checker_name = _normalize_standard_checker_name(raw_name)
    source = (_C.STANDARD_CHECKER_ROOT / checker_name).resolve()
    try:
        source.relative_to(_C.STANDARD_CHECKER_ROOT)
    except ValueError:
        raise ValueError('invalid standard checker name')
    try:
        if source.is_symlink() or not source.exists() or (not source.is_file()):
            raise ValueError(f'unknown standard checker: std::{checker_name}')
    except OSError:
        raise ValueError('standard checker catalog is unavailable')
    return (checker_name, source)

def _read_build_config(workspace: Path) -> tuple[dict, Path]:
    cfg_path = _safe_workspace_path(workspace, str(_C.BUILD_CONFIG_REL))
    payload: dict = {}
    if cfg_path.exists() and cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                payload = dict(raw)
        except Exception:
            payload = {}
    return (payload, cfg_path)

def _write_build_config(cfg_path: Path, payload: dict) -> None:
    data = dict(payload) if isinstance(payload, dict) else {}
    has_generator_keys = 'generator_sources' in data
    if has_generator_keys:
        normalized_sources = _generator_sources_from_build_cfg(data)
        if normalized_sources:
            data['generator_sources'] = normalized_sources
        else:
            data.pop('generator_sources', None)
    data.pop('generator_source', None)
    data.pop('accepted_source', None)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')

def _dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result

def _workspace_rel_file_exists(workspace: Path, rel: str | None) -> bool:
    normalized = _normalize_workspace_rel_path(rel)
    if not normalized:
        return False
    try:
        target = _safe_workspace_path(workspace, normalized)
    except HTTPException:
        return False
    try:
        if target.is_symlink():
            return False
        return bool(target.exists() and target.is_file())
    except OSError:
        return False

def _tests_spec_workspace_path(workspace: Path) -> Path:
    return _safe_workspace_path(workspace, TESTS_SPEC_REL.as_posix())

def _tests_spec_payload_rel_path(test_id: str, kind: str) -> str:
    safe_id = normalize_test_id(test_id)
    safe_kind = normalize_test_kind(kind)
    return payload_rel_path_for_test(safe_id, safe_kind)

def _tests_spec_payload_file_path(workspace: Path, test_id: str, kind: str) -> Path:
    return _safe_workspace_path(workspace, _tests_spec_payload_rel_path(test_id, kind))

def _tests_spec_read_payload(workspace: Path, entry: dict) -> str:
    test_id = str(entry.get('id') or '').strip()
    if not test_id:
        return ''
    kind = str(entry.get('kind') or '').strip().lower()
    try:
        payload_path = _tests_spec_payload_file_path(workspace, test_id, kind)
    except (HTTPException, ValueError):
        return ''
    try:
        if payload_path.exists() and payload_path.is_file() and (not payload_path.is_symlink()):
            return payload_path.read_text(encoding='utf-8')
    except OSError:
        return ''
    return ''

def _text_head_by_bytes(raw: str, max_bytes: int) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    encoded = str(raw or '').encode('utf-8', errors='replace')
    clipped = len(encoded) > cap
    head = encoded[:cap].decode('utf-8', errors='replace')
    return (head, clipped)

def _file_head_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    cap = max(1, int(max_bytes))
    try:
        with path.open('rb') as f:
            head = f.read(cap + 1)
    except OSError:
        return ('(unreadable)', False)
    clipped = len(head) > cap
    text = head[:cap].decode('utf-8', errors='replace')
    return (text, clipped)

def _run_detail_preview_unavailable(message: str='missing') -> dict[str, object]:
    return {'available': False, 'text': '', 'truncated': False, 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': '', 'message': str(message or 'missing')}

def _run_detail_preview_from_path(path: Path, download_href: str) -> dict[str, object]:
    text, clipped = _file_head_text(path, _C.RUN_DETAIL_PREVIEW_MAX_BYTES)
    normalized = _sanitize_log_text_for_ui(text)
    if not normalized:
        normalized = '(empty)'
    return {'available': True, 'text': normalized, 'truncated': bool(clipped), 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': str(download_href or ''), 'message': ''}

def _tests_spec_write_payload(workspace: Path, test_id: str, kind: str, content: str) -> None:
    safe_kind = normalize_test_kind(kind)
    payload_path = _tests_spec_payload_file_path(workspace, test_id, safe_kind)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(str(content), encoding='utf-8')
    stale_kind = 'gen' if safe_kind == 'manual' else 'manual'
    stale_payload_path = _tests_spec_payload_file_path(workspace, test_id, stale_kind)
    stale_payload_path.unlink(missing_ok=True)

def _tests_spec_remove_payload(workspace: Path, test_id: str) -> None:
    for kind in ('manual', 'gen'):
        try:
            payload_path = _tests_spec_payload_file_path(workspace, test_id, kind)
        except (HTTPException, ValueError):
            continue
        payload_path.unlink(missing_ok=True)

def _tests_spec_bool_flag(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or '').strip().lower()
    return text in {'1', 'true', 'yes', 'on'}

def _tests_spec_form_text(raw: object) -> str:
    if raw is None:
        return ''
    if raw.__class__.__module__.startswith('fastapi.params') and hasattr(raw, 'default'):
        raw = getattr(raw, 'default', '')
        if raw is None:
            return ''
    return str(raw)

def _read_tests_spec(workspace: Path) -> tuple[list[dict], Path]:
    path = _tests_spec_workspace_path(workspace)
    try:
        entries = load_tests_spec(path)
    except ValueError as exc:
        raise ValueError(f'invalid tests/spec.json: {exc}') from exc
    return (entries, path)

def _write_tests_spec(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_tests_spec(entries), encoding='utf-8')

def _tests_spec_editor_context(workspace: Path, limit: int=_C.TESTS_SPEC_ROWS_LIMIT) -> dict:
    entries, path = _read_tests_spec(workspace)
    summary = summarize_tests_spec(entries)
    rows: list[dict] = []
    cap = max(1, int(limit))
    truncated = len(entries) > cap
    for idx, entry in enumerate(entries[:cap], start=1):
        kind = str(entry.get('kind') or '')
        test_id = str(entry.get('id') or '').strip()
        sample = bool(entry.get('sample'))
        payload_path = _tests_spec_payload_rel_path(test_id, kind) if test_id and kind else ''
        payload_abs: Path | None = None
        if payload_path:
            try:
                payload_abs = _tests_spec_payload_file_path(workspace, test_id, kind)
            except (HTTPException, ValueError):
                payload_abs = None
        payload = ''
        preview_source = ''
        payload_size_bytes = 0
        manual_large_payload = False
        preview_clipped = False
        preview_bytes_limit = 0
        payload_file_exists = False
        if payload_abs is not None:
            try:
                payload_file_exists = bool(payload_abs.exists() and payload_abs.is_file() and (not payload_abs.is_symlink()))
            except OSError:
                payload_file_exists = False
        if payload_file_exists and payload_abs is not None:
            try:
                payload_size_bytes = max(0, int(payload_abs.stat().st_size))
            except OSError:
                payload_size_bytes = 0
        if kind == 'manual' and payload_size_bytes > _C.TESTS_SPEC_MANUAL_INLINE_EDIT_MAX_BYTES:
            manual_large_payload = True
            preview_bytes_limit = _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES
            if payload_file_exists and payload_abs is not None:
                preview_source, preview_clipped = _file_head_text(payload_abs, _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES)
            else:
                fallback_payload = _tests_spec_read_payload(workspace, entry)
                payload_size_bytes = len(fallback_payload.encode('utf-8', errors='replace'))
                preview_source, preview_clipped = _text_head_by_bytes(fallback_payload, _C.TESTS_SPEC_MANUAL_PREVIEW_BYTES)
        else:
            payload = _tests_spec_read_payload(workspace, entry)
            preview_source = payload
            if payload_size_bytes <= 0:
                payload_size_bytes = len(payload.encode('utf-8', errors='replace'))
        if manual_large_payload:
            preview_text = str(preview_source or '').replace('\r\n', '\n').replace('\r', '\n')
            if not preview_text:
                preview_text = '(empty)'
        else:
            preview_text = _inline_text_preview(preview_source, _C.TESTS_SPEC_PREVIEW_CHARS, _C.TESTS_SPEC_PREVIEW_LINES)
        rows.append({'index': idx, 'id': test_id, 'kind': kind, 'sample': sample, 'payload_path': payload_path, 'payload': payload, 'preview': preview_text, 'payload_size_bytes': payload_size_bytes, 'payload_size_human': _human_size(payload_size_bytes), 'manual_large_payload': manual_large_payload, 'preview_bytes_limit': preview_bytes_limit, 'preview_clipped': preview_clipped})
    return {'path': TESTS_SPEC_REL.as_posix(), 'exists': bool(path.exists() and path.is_file() and (not path.is_symlink())), 'entries': entries, 'rows': rows, 'summary': summary, 'total': len(entries), 'shown': len(rows), 'truncated': truncated}

def _tests_spec_status_context(workspace: Path) -> dict:
    try:
        entries, _path = _read_tests_spec(workspace)
    except ValueError:
        return {'mode': 'invalid', 'display': 'invalid', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    summary = summarize_tests_spec(entries)
    total = int(summary.get('total') or 0)
    manual = int(summary.get('manual') or 0)
    gen = int(summary.get('gen') or 0)
    sample = int(summary.get('sample') or 0)
    if total <= 0:
        return {'mode': 'empty', 'display': 'empty', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    return {'mode': 'ready', 'display': f'{total} ({sample} sample)', 'total': total, 'manual': manual, 'gen': gen, 'sample': sample}

def _list_cpp_sources(workspace: Path, folder: str, limit: int=64) -> tuple[list[str], bool]:
    base = workspace / folder
    try:
        if not base.exists() or not base.is_dir() or base.is_symlink():
            return ([], False)
    except OSError:
        return ([], False)
    names: list[str] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = str(entry.name or '')
                if Path(name).suffix.lower() not in _C.CPP_SOURCE_EXTENSIONS:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(f'{folder}/{name}')
    except OSError:
        return ([], False)
    names.sort()
    truncated = len(names) > int(limit)
    if truncated:
        names = names[:int(limit)]
    return (names, truncated)

def _list_solution_sources(workspace: Path, limit: int=64) -> tuple[list[str], bool]:
    base = workspace / 'solutions'
    try:
        if not base.exists() or not base.is_dir() or base.is_symlink():
            return ([], False)
    except OSError:
        return ([], False)
    names: list[str] = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                name = str(entry.name or '')
                if Path(name).suffix.lower() not in _C.SOLUTION_SOURCE_EXTENSIONS:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(f'solutions/{name}')
    except OSError:
        return ([], False)
    names.sort()
    truncated = len(names) > int(limit)
    if truncated:
        names = names[:int(limit)]
    return (names, truncated)

def _solution_behavior_options() -> list[dict]:
    return [{'value': value, 'label': expected_behavior_label(value)} for value in EXPECTED_BEHAVIOR_VALUES]

def _normalize_solution_source_path_required(raw: str | None) -> str:
    normalized = _normalize_workspace_rel_path(raw)
    if not normalized:
        raise ValueError('solution source is required')
    if not normalized.startswith('solutions/'):
        raise ValueError('solution source must be under solutions/')
    suffix = Path(normalized).suffix.lower()
    if suffix not in _C.SOLUTION_SOURCE_EXTENSIONS:
        raise ValueError('solution source must be .cpp/.cc/.cxx/.c++/.py/.java')
    return normalized

def _ensure_solution_metadata_for_source(workspace: Path, source_rel: str) -> bool:
    source = _normalize_solution_source_path_required(source_rel)
    expected = infer_expected_behavior_from_name(source)
    desc_rel = desc_rel_path_for_source(source)
    desc_abs = _safe_workspace_path(workspace, desc_rel)
    if desc_abs.exists() and desc_abs.is_file() and (desc_abs.stat().st_size > 0):
        return False
    config.git_service.write_file(workspace, desc_rel, render_solution_desc(expected, ''))
    return True

def _solution_metadata_entry(workspace: Path, source_rel: str) -> dict:
    source_path = str(source_rel or '')
    desc_path = desc_rel_path_for_source(source_path)
    desc_exists = _workspace_rel_file_exists(workspace, desc_path)
    expected = infer_expected_behavior_from_name(source_path)
    note = ''
    errors: list[str] = []
    origin = 'inferred' if expected != 'unknown' else 'default'
    if desc_exists:
        try:
            desc_abs = _safe_workspace_path(workspace, desc_path)
            desc_text, _ = _read_text_safe_limited(desc_abs, _C.SOLUTION_NOTE_CHAR_LIMIT * 8)
            parsed = parse_solution_desc(desc_text)
            expected = str(parsed.get('expected_behavior') or 'unknown')
            note = str(parsed.get('note') or '')
            origin = 'metadata'
            errors = [str(item) for item in parsed.get('errors') or []]
        except Exception as exc:
            errors = [str(exc)]
            origin = 'invalid'
    note_preview = note
    if len(note_preview) > 160:
        note_preview = note_preview[:157] + '...'
    return {'source_path': source_path, 'file_name': Path(source_path).name, 'expected_behavior': expected, 'expected_behavior_label': expected_behavior_label(expected), 'note': note, 'note_preview': note_preview, 'desc_path': desc_path, 'desc_exists': desc_exists, 'desc_origin': origin, 'desc_errors': errors, 'is_accepted': expected == 'accepted'}

def _list_solution_entries(workspace: Path) -> tuple[list[dict], bool]:
    sources, truncated = _list_solution_sources(workspace, limit=_C.SOLUTION_LIST_LIMIT)
    entries = [_solution_metadata_entry(workspace, rel) for rel in sources]
    return (entries, truncated)

def _resolve_build_accepted_solution_source(workspace: Path, entries: list[dict]) -> str:
    build_cfg, _ = _read_build_config(workspace)
    configured = _normalize_optional_component_source_path_safe(build_cfg.get('accepted_solution_source'), 'solutions', 'accepted solution source')
    if configured:
        return configured
    return ''

def _solutions_status_context(workspace: Path) -> dict:
    entries, truncated = _list_solution_entries(workspace)
    total = len(entries)
    accepted_source = _resolve_build_accepted_solution_source(workspace, entries)
    accepted_exists = bool(accepted_source) and _workspace_rel_file_exists(workspace, accepted_source)
    if truncated:
        count_display = f'{total}+ files'
    elif total == 1:
        count_display = '1 file'
    else:
        count_display = f'{total} files'
    if accepted_exists:
        mode = 'ready'
        display = f'{accepted_source} ({count_display})'
    elif total > 0:
        mode = 'missing-main'
        display = f'main missing ({count_display})'
    else:
        mode = 'missing'
        display = 'missing'
    return {'mode': mode, 'display': display, 'accepted_source': accepted_source, 'accepted_exists': accepted_exists, 'count': total, 'count_display': count_display, 'truncated': bool(truncated)}

def _run_solution_options_context(workspace: Path) -> tuple[list[dict], str, bool]:
    entries, truncated = _list_solution_entries(workspace)
    default_path = _resolve_build_accepted_solution_source(workspace, entries)
    if default_path and (not any((str(row.get('source_path') or '') == default_path for row in entries))):
        default_path = ''
    options: list[dict] = []
    for row in entries:
        path = str(row.get('source_path') or '').strip()
        if not path:
            continue
        behavior = str(row.get('expected_behavior_label') or '').strip()
        label = path if not behavior else f'{path} ({behavior})'
        options.append({'path': path, 'label': label, 'is_accepted': str(row.get('expected_behavior') or '') == 'accepted', 'expected_behavior': normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))})
    return (options, default_path, bool(truncated))

def _run_test_options_from_build(problem: str, build_id: str, limit: int=_C.RUN_TEST_SELECTOR_LIMIT) -> tuple[list[dict], bool]:
    options: list[dict] = []
    truncated = False
    try:
        root = _artifact_root(problem, build_id)
    except HTTPException:
        return (options, truncated)
    tests_dir = root / 'tests'
    try:
        if not tests_dir.exists() or not tests_dir.is_dir() or tests_dir.is_symlink():
            return (options, truncated)
    except OSError:
        return (options, truncated)
    names: list[str] = []
    try:
        with os.scandir(tests_dir) as entries:
            for entry in entries:
                name = str(entry.name or '')
                if not _C.RUN_TEST_NAME_RE.fullmatch(name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(name)
    except OSError:
        return (options, truncated)
    names.sort()
    cap = max(1, int(limit))
    truncated = len(names) > cap
    names = names[:cap]
    tests_meta_by_name: dict[str, dict] = {}
    tests_meta_path = root / 'logs' / 'tests_meta.json'
    try:
        if tests_meta_path.exists() and tests_meta_path.is_file() and (not tests_meta_path.is_symlink()):
            tests_meta_text, _ = _read_text_safe_limited(tests_meta_path, _C.SUMMARY_JSON_UI_CHAR_LIMIT)
            payload = json.loads(tests_meta_text)
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    index = _coerce_int(item.get('index'), 0, 1, 10 ** 7)
                    if index <= 0:
                        continue
                    tests_meta_by_name[f'{index:03d}.in'] = item
    except Exception:
        tests_meta_by_name = {}
    for name in names:
        item = tests_meta_by_name.get(name) or {}
        parts: list[str] = []
        test_id = str(item.get('id') or '').strip()
        if test_id:
            parts.append(f'id={test_id}')
        kind = str(item.get('kind') or '').strip().lower()
        if kind in {'manual', 'gen'}:
            parts.append(kind)
        if bool(item.get('sample')):
            parts.append('sample')
        desc = str(item.get('desc') or '').strip()
        if desc and desc not in {'manual', 'gen'}:
            parts.append(desc)
        suffix = f' ({'; '.join(parts)})' if parts else ''
        options.append({'name': name, 'label': f'{name}{suffix}'})
    return (options, truncated)

def _run_test_options_from_spec(workspace: Path, limit: int=_C.RUN_TEST_SELECTOR_LIMIT) -> tuple[list[dict], bool]:
    options: list[dict] = []
    try:
        entries, _ = _read_tests_spec(workspace)
    except Exception:
        return (options, False)
    cap = max(1, int(limit))
    truncated = len(entries) > cap
    for idx, row in enumerate(entries[:cap], start=1):
        name = f'{idx:03d}.in'
        parts: list[str] = []
        test_id = str(row.get('id') or '').strip()
        if test_id:
            parts.append(f'id={test_id}')
        kind = str(row.get('kind') or '').strip().lower()
        if kind in {'manual', 'gen'}:
            parts.append(kind)
        if _json_truthy(row.get('sample')):
            parts.append('sample')
        suffix = f' ({'; '.join(parts)})' if parts else ''
        options.append({'name': name, 'label': f'{name}{suffix}'})
    return (options, truncated)

def _run_test_options_context(problem: str, workspace: Path, active_build: dict | None) -> tuple[list[dict], bool, str]:
    build_id = str(active_build['id'] or '').strip() if active_build is not None else ''
    if build_id:
        build_options, build_truncated = _run_test_options_from_build(problem, build_id, limit=_C.RUN_TEST_SELECTOR_LIMIT)
        if build_options:
            return (build_options, build_truncated, f'build {build_id}')
    spec_options, spec_truncated = _run_test_options_from_spec(workspace, limit=_C.RUN_TEST_SELECTOR_LIMIT)
    if spec_options:
        return (spec_options, spec_truncated, 'tests/spec.json')
    return ([], False, '')

def _human_size(num_bytes: int) -> str:
    size = max(0, int(num_bytes))
    if size < 1024:
        return f'{size} B'
    value = float(size)
    for unit in ('KB', 'MB', 'GB'):
        value /= 1024.0
        if value < 1024.0 or unit == 'GB':
            return f'{value:.1f} {unit}'
    return f'{size} B'

def _inline_text_preview(raw: str, max_chars: int, max_lines: int) -> str:
    text = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    lines = [line.rstrip('\n\r') for line in text.splitlines()]
    clipped_by_lines = False
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[:max_lines]
        clipped_by_lines = True
    preview = '\n'.join(lines).strip()
    if not preview:
        preview = '(empty)'
    clipped_by_chars = len(preview) > max_chars
    if clipped_by_chars:
        preview = preview[:max_chars - 3].rstrip() + '...'
    elif clipped_by_lines:
        preview += ' ...'
    return preview

def _latest_workspace_build(problem_id: int, workspace_id: int, *, ok_only: bool=False):
    sql = 'SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? AND workspace_id=?'
    params: list[object] = [problem_id, workspace_id]
    if ok_only:
        sql += " AND status='ok'"
    sql += ' ORDER BY created_at DESC LIMIT 1'
    return config.db.fetch_one(sql, params)

def _latest_workspace_committed_build(problem_id: int, workspace_id: int, head_commit: str, *, ok_only: bool=False):
    commit = str(head_commit or '').strip()
    if not commit:
        return None
    sql = 'SELECT id,status,source_commit,source_ref,created_at,finished_at FROM builds WHERE problem_id=? AND workspace_id=? AND source_commit=? AND source_ref=?'
    params: list[object] = [problem_id, workspace_id, commit, commit]
    if ok_only:
        sql += " AND status='ok'"
    sql += ' ORDER BY created_at DESC LIMIT 1'
    return config.db.fetch_one(sql, params)

def _json_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or '').strip().lower()
    return text in {'1', 'true', 'yes', 'on'}

_VERIFICATION_SIGNATURE_FILE_TARGETS: tuple[str, ...] = (
    'config/problem.json',
    'config/build.json',
    'tests/spec.json',
)
_VERIFICATION_SIGNATURE_DIR_TARGETS: tuple[str, ...] = (
    'generators',
    'validators',
    'checkers',
    'solutions',
    'tests/manual',
    'tests/generator',
)
_VERIFICATION_SIGNATURE_DETAIL_TARGETS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ('general info', ('config/problem.json',), ()),
    ('build config', ('config/build.json',), ()),
    ('generators', (), ('generators',)),
    ('validator', (), ('validators',)),
    ('checker', (), ('checkers',)),
    ('solutions', (), ('solutions',)),
    ('tests', ('tests/spec.json',), ('tests/manual', 'tests/generator')),
)
_VERIFICATION_SIGNATURE_CHUNK_SIZE = 1024 * 64

def _verification_sources_signature_from_targets(workspace: Path, file_targets: tuple[str, ...], dir_targets: tuple[str, ...]) -> str:
    hasher = hashlib.sha256()
    try:
        workspace_resolved = workspace.resolve()
    except OSError:
        workspace_resolved = workspace

    def _safe_file(rel_path: str) -> Path | None:
        target = workspace / rel_path
        try:
            if target.is_symlink() or not target.exists() or not target.is_file():
                return None
            resolved = target.resolve()
        except OSError:
            return None
        if workspace_resolved not in resolved.parents and workspace_resolved != resolved:
            return None
        return target

    def _hash_file(rel_path: str, target: Path | None) -> None:
        hasher.update(f'file:{rel_path}'.encode('utf-8', errors='replace'))
        hasher.update(b'\0')
        if target is None:
            hasher.update(b'[missing]')
            hasher.update(b'\0')
            return
        try:
            with target.open('rb') as handle:
                while True:
                    chunk = handle.read(_VERIFICATION_SIGNATURE_CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
        except OSError:
            hasher.update(b'[unreadable]')
        hasher.update(b'\0')

    def _hash_dir(rel_dir: str) -> None:
        hasher.update(f'dir:{rel_dir}'.encode('utf-8', errors='replace'))
        hasher.update(b'\0')
        root = workspace / rel_dir
        try:
            if root.is_symlink() or not root.exists() or not root.is_dir():
                hasher.update(b'[missing]')
                hasher.update(b'\0')
                return
            root_resolved = root.resolve()
        except OSError:
            hasher.update(b'[missing]')
            hasher.update(b'\0')
            return
        if workspace_resolved not in root_resolved.parents and workspace_resolved != root_resolved:
            hasher.update(b'[invalid]')
            hasher.update(b'\0')
            return
        files: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dir_root = Path(dirpath)
            try:
                dir_root_resolved = dir_root.resolve()
            except OSError:
                dirnames[:] = []
                continue
            if workspace_resolved not in dir_root_resolved.parents and workspace_resolved != dir_root_resolved:
                dirnames[:] = []
                continue
            safe_dirs: list[str] = []
            for name in dirnames:
                child = dir_root / name
                try:
                    if child.is_symlink() or not child.exists() or not child.is_dir():
                        continue
                except OSError:
                    continue
                safe_dirs.append(name)
            dirnames[:] = sorted(safe_dirs)
            for name in sorted(filenames):
                path = dir_root / name
                try:
                    if path.is_symlink() or not path.exists() or not path.is_file():
                        continue
                    path_resolved = path.resolve()
                except OSError:
                    continue
                if workspace_resolved not in path_resolved.parents and workspace_resolved != path_resolved:
                    continue
                try:
                    rel = path.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                files.append((rel, path))
        files.sort(key=lambda item: item[0])
        for rel, path in files:
            hasher.update(rel.encode('utf-8', errors='replace'))
            hasher.update(b'\0')
            try:
                with path.open('rb') as handle:
                    while True:
                        chunk = handle.read(_VERIFICATION_SIGNATURE_CHUNK_SIZE)
                        if not chunk:
                            break
                        hasher.update(chunk)
            except OSError:
                hasher.update(b'[unreadable]')
            hasher.update(b'\0')
        hasher.update(b'\0')

    for rel_path in file_targets:
        _hash_file(rel_path, _safe_file(rel_path))
    for rel_dir in dir_targets:
        _hash_dir(rel_dir)
    return hasher.hexdigest()

def _verification_sources_signature(workspace: Path) -> str:
    return _verification_sources_signature_from_targets(workspace, _VERIFICATION_SIGNATURE_FILE_TARGETS, _VERIFICATION_SIGNATURE_DIR_TARGETS)

def _verification_sources_signature_details(workspace: Path) -> dict[str, str]:
    details: dict[str, str] = {}
    for label, file_targets, dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
        details[label] = _verification_sources_signature_from_targets(workspace, file_targets, dir_targets)
    return details

def _verification_run_passed(run_status: str, summary: dict | None) -> bool:
    if str(run_status or '').strip().lower() != 'ok':
        return False
    if not isinstance(summary, dict):
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests')
    if not isinstance(tests, list) or not tests:
        return False
    for row in tests:
        if not isinstance(row, dict):
            return False
        if str(row.get('verdict') or '').strip().upper() != 'OK':
            return False
    return True

def _verification_run_completed(run_status: str, summary: dict | None) -> bool:
    if str(run_status or '').strip().lower() != 'ok':
        return False
    if not isinstance(summary, dict):
        return False
    if summary.get('error'):
        return False
    tests = summary.get('tests')
    if not isinstance(tests, list) or not tests:
        return False
    return True

def _verification_solution_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, bool, bool, str]:
    expected = normalize_expected_behavior(expected_behavior)
    completed = _verification_run_completed(run_status, summary)
    observed_pass = _verification_run_passed(run_status, summary)
    observed_short = _run_actual_short(run_status, summary)
    observed_failed_codes = _run_actual_failed_codes(run_status, summary)
    observed_display = '/'.join(observed_failed_codes) if observed_failed_codes else (observed_short or '--')
    if expected == 'accepted':
        matched = completed and observed_pass
        reason = '' if matched else 'accepted solution must pass all tests'
        return (matched, completed, observed_pass, reason)
    if expected in {'wrong_answer', 'time_limit_exceeded', 'run_time_error'}:
        expected_short = _run_expected_short(expected)
        matched = completed and bool(observed_failed_codes) and all((code == expected_short for code in observed_failed_codes))
        reason = '' if matched else f'expected {expected_short}, got {observed_display}'
        return (matched, completed, observed_pass, reason)
    if expected == 'rejected':
        matched = completed and bool(observed_failed_codes)
        reason = '' if matched else f'expected rejected, got {observed_display}'
        return (matched, completed, observed_pass, reason)
    if expected == 'unknown':
        matched = completed
        reason = '' if matched else 'solution run did not complete'
        return (matched, completed, observed_pass, reason)
    matched = completed and (not observed_pass)
    reason = '' if matched else 'non-accepted solution must fail at least one test'
    return (matched, completed, observed_pass, reason)

def _verification_solution_failure_hint(source_path: str, reason: str, error_text: str='') -> str:
    source_label = _source_basename_label(source_path) or str(source_path or '').strip() or 'solution'
    reason_text = str(reason or '').strip()
    compact_error = _compact_error_text(str(error_text or ''))
    if reason_text and compact_error:
        detail = f'{reason_text}: {compact_error}'
    elif reason_text:
        detail = reason_text
    elif compact_error:
        detail = compact_error
    else:
        detail = 'verification mismatch'
    return f'{source_label}: {detail}'

def _verification_first_unmatched_hint(solutions: object) -> str:
    if not isinstance(solutions, list):
        return ''
    for item in solutions:
        if not isinstance(item, dict):
            continue
        if bool(item.get('matched')):
            continue
        hint = _verification_solution_failure_hint(
            str(item.get('source_path') or ''),
            str(item.get('reason') or ''),
            str(item.get('error') or ''),
        )
        if hint:
            return hint
    return ''

def _verification_stale_reason(changed_components: list[str], *, head_changed: bool, dirty_changed: bool) -> str:
    parts: list[str] = [str(name or '').strip() for name in changed_components if str(name or '').strip()]
    if not parts:
        if head_changed:
            parts.append('workspace revision')
        if dirty_changed:
            parts.append('working copy')
    if not parts:
        return 'changed: verification inputs'
    return 'changed: ' + ', '.join(parts)

def _verification_status_context(
    problem_id: int,
    actor_user_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    workspace_path: Path | str | None=None,
) -> dict:
    row = config.db.fetch_one("\n        SELECT details_json,created_at\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action='verification.start'\n        ORDER BY created_at DESC\n        LIMIT 1\n        ", [problem_id, actor_user_id])
    if row is None:
        return {'mode': 'none', 'display': 'none', 'last_status': 'none', 'run_id': '', 'run_ids': '', 'build_id': '', 'error': '', 'created_at': '', 'stale': False, 'stale_reason': ''}
    details: dict = {}
    try:
        parsed = json.loads(str(row['details_json'] or '{}'))
        if isinstance(parsed, dict):
            details = parsed
    except Exception:
        details = {}
    last_status = str(details.get('status') or '').strip().lower()
    if last_status not in {'pass', 'failed', 'running'}:
        last_status = 'failed'
    run_id = _normalize_run_id_token(details.get('run_id'))
    run_ids: list[str] = []
    raw_run_ids = details.get('run_ids')
    if isinstance(raw_run_ids, list):
        for item in raw_run_ids:
            token = _normalize_run_id_token(str(item or ''))
            if token:
                run_ids.append(token)
    elif isinstance(raw_run_ids, str):
        for item in str(raw_run_ids).split(','):
            token = _normalize_run_id_token(item)
            if token:
                run_ids.append(token)
    run_ids = _dedupe_preserve_order(run_ids)
    if run_id and run_id not in run_ids:
        run_ids.insert(0, run_id)
    if not run_id and run_ids:
        run_id = run_ids[0]
    recorded_signature = str(details.get('verification_signature') or '').strip()
    recorded_signature_details: dict[str, str] = {}
    raw_recorded_details = details.get('verification_signature_details')
    if isinstance(raw_recorded_details, dict):
        for key, value in raw_recorded_details.items():
            label = str(key or '').strip()
            signature = str(value or '').strip()
            if label and signature:
                recorded_signature_details[label] = signature
    current_signature = ''
    current_signature_details: dict[str, str] = {}
    if workspace_path:
        try:
            workspace_obj = Path(str(workspace_path))
            current_signature = _verification_sources_signature(workspace_obj)
            current_signature_details = _verification_sources_signature_details(workspace_obj)
        except Exception:
            current_signature = ''
            current_signature_details = {}
    recorded_head = str(details.get('workspace_head') or '').strip()
    recorded_dirty = _json_truthy(details.get('workspace_dirty'))
    stale = False
    head_changed = False
    dirty_changed = bool(workspace_dirty) != recorded_dirty
    changed_components: list[str] = []
    if recorded_signature and current_signature:
        stale = recorded_signature != current_signature
        if stale and recorded_signature_details and current_signature_details:
            for label, _file_targets, _dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
                if recorded_signature_details.get(label, '') != current_signature_details.get(label, ''):
                    changed_components.append(label)
    else:
        if recorded_head and workspace_head and (recorded_head != workspace_head):
            stale = True
            head_changed = True
        if dirty_changed:
            stale = True
        if stale and recorded_signature_details and current_signature_details:
            for label, _file_targets, _dir_targets in _VERIFICATION_SIGNATURE_DETAIL_TARGETS:
                if recorded_signature_details.get(label, '') != current_signature_details.get(label, ''):
                    changed_components.append(label)
    error_text = str(details.get('error') or '').strip()
    unmatched_hint = _verification_first_unmatched_hint(details.get('solutions'))
    if last_status == 'failed' and unmatched_hint:
        error_text = unmatched_hint
    elif not error_text and unmatched_hint:
        error_text = unmatched_hint
    mode = 'stale' if stale else last_status
    stale_reason = _verification_stale_reason(changed_components, head_changed=head_changed, dirty_changed=dirty_changed) if stale else ''
    return {'mode': mode, 'display': mode, 'last_status': last_status, 'run_id': run_id, 'run_ids': ','.join(run_ids), 'build_id': str(details.get('build_id') or '').strip(), 'error': error_text, 'created_at': str(row['created_at'] or ''), 'stale': stale, 'stale_reason': stale_reason}

def _ensure_implicit_build(problem: str, user: str, *, ctx: dict | None=None, force: bool=False) -> tuple[str, bool]:
    local_ctx = ctx or page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    problem_id = int(local_ctx['problem']['id'])
    workspace_id = int(local_ctx['workspace']['id'])
    head_commit = str(local_ctx['workspace'].get('head_commit') or '').strip()
    branch = str(local_ctx['workspace'].get('branch') or 'main').strip() or 'main'
    dirty = bool(local_ctx['workspace'].get('dirty'))
    latest_ok = _latest_workspace_build(problem_id, workspace_id, ok_only=True)
    if not force and latest_ok is not None:
        latest_id = str(latest_ok['id'] or '').strip()
        latest_commit = str(latest_ok['source_commit'] or '').strip()
        latest_ref = str(latest_ok['source_ref'] or '').strip()
        if latest_id and latest_commit and (latest_commit == head_commit) and (latest_ref == branch) and (not dirty):
            return (latest_id, False)
        if latest_id and latest_commit and (latest_commit == head_commit) and dirty:
            created_at = _parse_iso_utc(str(latest_ok['created_at'] or ''))
            if created_at is not None and (_utc_now() - created_at).total_seconds() <= _C.IMPLICIT_BUILD_DIRTY_REUSE_SEC:
                return (latest_id, False)
    build_id = config.build_service.run_build(problem, user)
    return (build_id, True)

def _allocate_run_id() -> str:
    for _ in range(8):
        candidate = f'r-{secrets.token_hex(6)}'
        if config.db.fetch_one('SELECT id FROM runs WHERE id=?', [candidate]) is None:
            return candidate
    return f'r-{secrets.token_hex(8)}'

def _allocate_invocation_id() -> str:
    return f'inv-{secrets.token_hex(6)}'

def _record_async_run_failure(problem: str, user: str, run_id: str, *, mode: str, source_label: str, error: str, build_id: str, invocation_id: str='', invocation_run_ids: list[str] | None=None, expected_behavior: str='unknown', invocation_source: str='run.execute') -> None:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return
    safe_mode = _normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    safe_source = str(source_label or 'upload').strip() or 'upload'
    safe_error = str(error or 'invocation failed').strip() or 'invocation failed'
    safe_expected = normalize_expected_behavior(expected_behavior)
    safe_invocation_id = str(invocation_id or '').strip()
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', safe_invocation_id):
        safe_invocation_id = ''
    safe_invocation_run_ids: list[str] = []
    raw_run_ids = invocation_run_ids or []
    for raw in raw_run_ids:
        token = str(raw or '').strip()
        if not re.fullmatch('[A-Za-z0-9._-]{1,80}', token):
            continue
        safe_invocation_run_ids.append(token)
    safe_invocation_run_ids = _dedupe_preserve_order(safe_invocation_run_ids)
    if safe_run_id not in safe_invocation_run_ids:
        safe_invocation_run_ids.append(safe_run_id)
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    except Exception:
        return
    run_root = (Path(config.workspace_service.config.settings.run_root) / 'invalid-runs' / safe_run_id).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    compile_log_name = 'compile.log'
    (run_root / compile_log_name).write_text(safe_error + '\n', encoding='utf-8')
    summary = {'error': safe_error, 'mode': safe_mode, 'source': safe_source, 'tests': [], 'compile_log': compile_log_name, 'compile_diagnostics': [], 'toolchain_digest': 'unknown', 'sandbox_backend': config.sandbox_backend.name, 'invocation_backend': config.invocation_backend_service.active_backend_name(), 'limits': {}, 'usage': {}}
    if safe_invocation_id:
        matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, 'failed', summary)
        summary['invocation'] = {'id': safe_invocation_id, 'source': str(invocation_source or 'run.execute').strip() or 'run.execute', 'run_ids': safe_invocation_run_ids, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}
    (run_root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    now = now_iso()
    existing = config.db.fetch_one('SELECT id FROM runs WHERE id=?', [safe_run_id])
    safe_build_id = str(build_id or '').strip() or _C.RUN_PLACEHOLDER_BUILD_ID
    if existing is None:
        config.db.execute('\n            INSERT INTO runs(\n                id,problem_id,workspace_id,build_id,mode,status,summary_json,artifact_path,created_at,finished_at\n            ) VALUES(?,?,?,?,?,?,?,?,?,?)\n            ', [safe_run_id, int(ctx['problem']['id']), int(ctx['workspace']['id']), safe_build_id, safe_mode, 'failed', json.dumps(summary), str(run_root), now, now])
        return
    config.db.execute('\n        UPDATE runs\n        SET build_id=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=?\n        WHERE id=?\n        ', [safe_build_id, safe_mode, 'failed', json.dumps(summary), str(run_root), now, safe_run_id])

def _annotate_run_invocation_result(problem_id: int, workspace_id: int, run_id: str, *, invocation_id: str, invocation_run_ids: list[str], expected_behavior: str, invocation_source: str='run.execute') -> dict[str, object]:
    safe_run_id = str(run_id or '').strip()
    safe_invocation_id = _normalize_run_id_token(invocation_id) or safe_run_id
    safe_expected = normalize_expected_behavior(expected_behavior)
    safe_run_ids = _dedupe_preserve_order([_normalize_run_id_token(item) for item in invocation_run_ids if _normalize_run_id_token(item)])
    if safe_run_id and safe_run_id not in safe_run_ids:
        safe_run_ids.append(safe_run_id)
    row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [safe_run_id, int(problem_id), int(workspace_id)])
    run_status = str(row['status'] or '').strip().lower() if row is not None else 'missing'
    summary_obj = _parse_summary_json(row['summary_json'] if row is not None else None, f'invocation/{safe_run_id}')
    if not isinstance(summary_obj, dict):
        summary_obj = {}
    matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, run_status, summary_obj)
    summary_obj['invocation'] = {'id': safe_invocation_id, 'source': str(invocation_source or 'run.execute').strip() or 'run.execute', 'run_ids': safe_run_ids, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}
    if row is not None:
        config.db.execute('UPDATE runs SET summary_json=? WHERE id=?', [json.dumps(summary_obj), safe_run_id])
    return {'run_id': safe_run_id, 'status': run_status, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}

def _run_execute_batch_worker(problem: str, user: str, *, requested_build_id: str, run_mode: str, targets: list[dict[str, object]], invocation_id: str, invocation_run_ids: list[str], selected_test_names: list[str]) -> None:
    resolved_build_id = str(requested_build_id or '').strip()
    try:
        ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
        problem_id = int(ctx['problem']['id'])
        workspace_id = int(ctx['workspace']['id'])
        if not resolved_build_id:
            resolved_build_id, _ = _ensure_implicit_build(problem, user, ctx=ctx, force=False)
        if not resolved_build_id:
            raise RuntimeError('tests generation did not produce a runnable build')
        _assert_workspace_build_access(ctx, resolved_build_id)
    except Exception as exc:
        err = str(exc)
        failed_build_id = resolved_build_id or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            _record_async_run_failure(problem, user, str(target.get('run_id') or ''), mode=run_mode, source_label=str(target.get('source_label') or ''), error=err, build_id=failed_build_id, invocation_id=invocation_id, invocation_run_ids=invocation_run_ids, expected_behavior=str(target.get('expected_behavior') or 'unknown'))
        _cleanup_runtime_cache(force=False)
        return
    for target in targets:
        run_id = str(target.get('run_id') or '').strip()
        if not run_id:
            continue
        source_label = str(target.get('source_label') or '').strip() or 'upload'
        submission_path_raw = str(target.get('submission_path') or '').strip()
        submission_path_arg = submission_path_raw or None
        upload_filename = str(target.get('upload_filename') or '').strip() or None
        expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
        raw_upload = target.get('upload_content')
        upload_content: bytes | None = None
        if isinstance(raw_upload, bytes):
            upload_content = raw_upload
        elif isinstance(raw_upload, bytearray):
            upload_content = bytes(raw_upload)
        try:
            if selected_test_names:
                config.invocation_backend_service.run_submission(
                    problem=problem,
                    username=user,
                    build_id=resolved_build_id,
                    submission_path=submission_path_arg,
                    mode=run_mode,
                    upload_content=upload_content,
                    upload_filename=upload_filename,
                    run_id=run_id,
                    selected_tests=selected_test_names,
                    invocation_id=invocation_id,
                    invocation_run_ids=invocation_run_ids,
                    expected_behavior=expected_behavior,
                    invocation_source='run.execute',
                )
            else:
                config.invocation_backend_service.run_submission(
                    problem=problem,
                    username=user,
                    build_id=resolved_build_id,
                    submission_path=submission_path_arg,
                    mode=run_mode,
                    upload_content=upload_content,
                    upload_filename=upload_filename,
                    run_id=run_id,
                    invocation_id=invocation_id,
                    invocation_run_ids=invocation_run_ids,
                    expected_behavior=expected_behavior,
                    invocation_source='run.execute',
                )
            _annotate_run_invocation_result(problem_id, workspace_id, run_id, invocation_id=invocation_id, invocation_run_ids=invocation_run_ids, expected_behavior=expected_behavior, invocation_source='run.execute')
        except Exception as exc:
            _record_async_run_failure(problem, user, run_id, mode=run_mode, source_label=source_label, error=str(exc), build_id=resolved_build_id, invocation_id=invocation_id, invocation_run_ids=invocation_run_ids, expected_behavior=expected_behavior)
    _cleanup_runtime_cache(force=False)

def _start_run_execute_batch(problem: str, user: str, *, requested_build_id: str, run_mode: str, targets: list[dict[str, object]], invocation_id: str, invocation_run_ids: list[str], selected_test_names: list[str]) -> bool:
    batch_id = str(invocation_id or targets[0].get('run_id') or 'invocation').strip() if targets else 'invocation'
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_execute_batch_worker(problem=problem, user=user, requested_build_id=requested_build_id, run_mode=run_mode, targets=targets, invocation_id=invocation_id, invocation_run_ids=invocation_run_ids, selected_test_names=selected_test_names)
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.run_execute_lock:
                    config.run_execute_workers.discard(worker)
    worker, queued = config.worker_queue_service.submit(
        name=f'run-execute-{batch_id}',
        fn=_runner,
        queue_name='invocation',
        backend=config.invocation_backend_service.active_backend_name(),
    )
    worker_ref[0] = worker
    if queued:
        with config.run_execute_lock:
            config.run_execute_workers.add(worker)
    return bool(queued)

def _wait_for_run_execute_workers(timeout_sec: float=300.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        with config.run_execute_lock:
            workers = [w for w in config.run_execute_workers if w.is_alive()]
            config.run_execute_workers.clear()
            config.run_execute_workers.update(workers)
        if not workers:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        config.worker_queue_service.wait_for_futures(workers, timeout_sec=min(0.2, remaining))

def _verification_workspace_key(problem_id: int, workspace_id: int) -> str:
    return f'{int(problem_id)}:{int(workspace_id)}'

def _run_verification_start_worker(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, str]],
    invocation_id: str,
    verification_signature: str='',
    verification_signature_details: dict[str, str] | None=None,
) -> None:
    planned_run_ids: list[str] = []
    for target in targets:
        token = _normalize_run_id_token(target.get('run_id'))
        if token and token not in planned_run_ids:
            planned_run_ids.append(token)
    run_ids: list[str] = list(planned_run_ids)
    run_id = run_ids[0] if run_ids else ''
    verification_details: dict[str, object] = {'status': 'failed', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'submission_paths': [str(item.get('path') or '') for item in targets], 'solution_count': len(targets), 'invocation_id': invocation_id, 'run_id': run_id, 'run_ids': list(run_ids), 'run_count': len(run_ids), 'invocation_backend': config.invocation_backend_service.active_backend_name(), 'error': ''}
    if verification_signature:
        verification_details['verification_signature'] = verification_signature
    if isinstance(verification_signature_details, dict) and verification_signature_details:
        verification_details['verification_signature_details'] = dict(verification_signature_details)
    try:
        build_id = config.build_service.run_build(problem, user)
        verification_details['build_id'] = build_id
        build_row = config.db.fetch_one('SELECT status,source_commit,source_ref,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [build_id, problem_id, workspace_id])
        build_status = str(build_row['status'] or 'missing').strip().lower() if build_row is not None else 'missing'
        verification_details['build_status'] = build_status
        verification_details['source_commit'] = str(build_row['source_commit'] or '').strip() if build_row is not None else ''
        verification_details['source_ref'] = str(build_row['source_ref'] or '').strip() if build_row is not None else ''
        build_summary = _parse_summary_json(build_row['summary_json'] if build_row is not None else None, f'verification/build/{build_id}')
        build_error = str(build_summary.get('error') or '').strip() if isinstance(build_summary, dict) else ''
        build_failed_step = str(build_summary.get('failed_step') or '').strip() if isinstance(build_summary, dict) else ''
        build_failed_test = str(build_summary.get('failed_test') or '').strip() if isinstance(build_summary, dict) else ''
        verification_details['build_error'] = build_error
        verification_details['build_failed_step'] = build_failed_step
        verification_details['build_failed_test'] = build_failed_test
        if build_status != 'ok':
            detail = _compact_error_text(build_error)
            if not detail and build_failed_step and build_failed_test:
                detail = f'{build_failed_step} failed on {build_failed_test}'
            elif not detail and build_failed_step:
                detail = f'{build_failed_step} failed'
            if detail:
                raise RuntimeError(f'build failed: {detail}')
            raise RuntimeError('build failed')
        solution_results: list[dict[str, object]] = []
        first_reason = ''
        for target in targets:
            source_path = str(target.get('path') or '').strip()
            expected_behavior = normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown'))
            requested_run_id = _normalize_run_id_token(target.get('run_id'))
            current_run_id = config.invocation_backend_service.run_submission(
                problem=problem,
                username=user,
                build_id=build_id,
                submission_path=source_path,
                mode='pass-fail',
                run_id=requested_run_id or None,
                invocation_id=invocation_id,
                invocation_run_ids=run_ids,
                expected_behavior=expected_behavior,
                invocation_source='verification.start',
            )
            current_run_id = _normalize_run_id_token(current_run_id) or current_run_id
            if requested_run_id and current_run_id and requested_run_id != current_run_id:
                run_ids = [current_run_id if token == requested_run_id else token for token in run_ids]
            if current_run_id and current_run_id not in run_ids:
                run_ids.append(current_run_id)
            run_ids = _dedupe_preserve_order(run_ids)
            target['run_id'] = current_run_id
            run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [current_run_id, problem_id, workspace_id])
            run_status = str(run_row['status'] or 'missing').strip().lower() if run_row is not None else 'missing'
            summary_obj = _parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{current_run_id}')
            matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, run_status, summary_obj)
            error_text = str(summary_obj.get('error') or '') if isinstance(summary_obj, dict) else ''
            if not matched and (not first_reason):
                first_reason = _verification_solution_failure_hint(source_path, reason, error_text)
            solution_results.append({'source_path': source_path, 'expected_behavior': expected_behavior, 'run_id': current_run_id, 'run_status': run_status, 'completed': completed, 'passed_all_tests': observed_pass, 'matched': matched, 'reason': reason, 'error': error_text})
        run_id = run_ids[0] if run_ids else ''
        verification_details['run_id'] = run_id
        verification_details['run_ids'] = list(run_ids)
        verification_details['solutions'] = solution_results
        verification_details['run_count'] = len(run_ids)
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
        verification_details['status'] = 'pass' if passed else 'failed'
        if not passed and first_reason:
            verification_details['error'] = first_reason
        for item in solution_results:
            current_run_id = str(item.get('run_id') or '').strip()
            if not current_run_id:
                continue
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            annotated = _annotate_run_invocation_result(problem_id, workspace_id, current_run_id, invocation_id=invocation_id, invocation_run_ids=run_ids, expected_behavior=expected_behavior, invocation_source='verification.start')
            item['matched'] = bool(annotated.get('matched'))
            item['completed'] = bool(annotated.get('completed'))
            item['passed_all_tests'] = bool(annotated.get('passed_all_tests'))
            item['reason'] = str(annotated.get('reason') or item.get('reason') or '')
        passed = bool(solution_results) and all((bool(item.get('matched')) for item in solution_results))
        verification_details['status'] = 'pass' if passed else 'failed'
        if not passed:
            unmatched = next((item for item in solution_results if (isinstance(item, dict) and (not bool(item.get('matched'))))), None)
            if isinstance(unmatched, dict):
                reason_first = _verification_solution_failure_hint(
                    str(unmatched.get('source_path') or ''),
                    str(unmatched.get('reason') or ''),
                    str(unmatched.get('error') or ''),
                )
                if reason_first:
                    verification_details['error'] = reason_first
        if run_id:
            run_row = config.db.fetch_one('SELECT summary_json FROM runs WHERE id=?', [run_id])
            run_summary_obj = _parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{run_id}/status')
            if not isinstance(run_summary_obj, dict):
                run_summary_obj = {}
            run_summary_obj['verification'] = {'source': 'sidebar', 'status': verification_details['status'], 'steps': verification_details['steps'], 'build_id': build_id, 'submission_paths': verification_details.get('submission_paths', []), 'run_ids': run_ids, 'source_commit': verification_details.get('source_commit', ''), 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty}
            config.db.execute('UPDATE runs SET summary_json=? WHERE id=?', [json.dumps(run_summary_obj), run_id])
    except Exception as exc:
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
    _audit(actor_user_id, problem_id, 'verification.start', verification_details)
    _cleanup_runtime_cache(force=False)

def _start_verification_job(
    problem: str,
    user: str,
    *,
    actor_user_id: int,
    problem_id: int,
    workspace_id: int,
    workspace_head: str,
    workspace_dirty: bool,
    targets: list[dict[str, str]],
    invocation_id: str,
    initial_details: dict[str, object] | None=None,
    workspace_path: Path | str | None=None,
) -> bool:
    key = _verification_workspace_key(problem_id, workspace_id)
    verification_signature = ''
    verification_signature_details: dict[str, str] = {}
    if workspace_path:
        try:
            workspace_obj = Path(str(workspace_path))
            verification_signature = _verification_sources_signature(workspace_obj)
            verification_signature_details = _verification_sources_signature_details(workspace_obj)
        except Exception:
            verification_signature = ''
            verification_signature_details = {}
    if isinstance(initial_details, dict) and verification_signature and (not str(initial_details.get('verification_signature') or '').strip()):
        initial_details['verification_signature'] = verification_signature
    if isinstance(initial_details, dict) and verification_signature_details and (not isinstance(initial_details.get('verification_signature_details'), dict)):
        initial_details['verification_signature_details'] = dict(verification_signature_details)
    with config.verification_lock:
        if key in config.verification_inflight:
            return False
        config.verification_inflight.add(key)
    if isinstance(initial_details, dict):
        try:
            _audit(actor_user_id, problem_id, 'verification.start', initial_details)
        except Exception:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_verification_start_worker(
                problem,
                user,
                actor_user_id=actor_user_id,
                problem_id=problem_id,
                workspace_id=workspace_id,
                workspace_head=workspace_head,
                workspace_dirty=workspace_dirty,
                targets=targets,
                invocation_id=invocation_id,
                verification_signature=verification_signature,
                verification_signature_details=verification_signature_details,
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.verification_lock:
                    config.verification_workers.discard(worker)
                    config.verification_inflight.discard(key)
    thread_name = invocation_id if invocation_id else key.replace(':', '-')
    try:
        worker, queued = config.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            backend=config.invocation_backend_service.active_backend_name(),
            dedupe_key=f'verification:{key}',
        )
        worker_ref[0] = worker
        if not queued:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            return False
        with config.verification_lock:
            config.verification_workers.add(worker)
    except Exception:
        with config.verification_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.verification_workers.discard(worker)
            config.verification_inflight.discard(key)
        raise
    return True

def _wait_for_verification_workers(timeout_sec: float=300.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        with config.verification_lock:
            workers = [w for w in config.verification_workers if w.is_alive()]
            config.verification_workers.clear()
            config.verification_workers.update(workers)
        if not workers:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        config.worker_queue_service.wait_for_futures(workers, timeout_sec=min(0.2, remaining))

def _run_preview_worker(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, workspace_head: str, workspace_dirty: bool) -> None:
    details: dict[str, object] = {'status': 'failed', 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'preview_id': '', 'preview_status': 'missing', 'source_commit': '', 'source_ref': '', 'error': ''}
    worker_error: Exception | None = None
    try:
        preview_id = config.preview_service.compile_preview(problem, user)
        details['preview_id'] = preview_id
        row = config.db.fetch_one('SELECT status,source_commit,source_ref,summary_json FROM previews WHERE id=? AND problem_id=? AND workspace_id=?', [preview_id, int(problem_id), int(workspace_id)])
        if row is None:
            raise RuntimeError('preview metadata missing after compile')
        preview_status = str(row['status'] or 'missing').strip().lower()
        details['preview_status'] = preview_status
        details['source_commit'] = str(row['source_commit'] or '').strip()
        details['source_ref'] = str(row['source_ref'] or '').strip()
        summary_obj = _parse_summary_json(row['summary_json'], f'preview/{preview_id}')
        if preview_status == 'ok':
            details['status'] = 'ok'
        else:
            details['status'] = 'failed'
            details['error'] = str(summary_obj.get('error') or 'preview failed') if isinstance(summary_obj, dict) else 'preview failed'
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        worker_error = exc
    _audit(actor_user_id, problem_id, 'preview.run', details)
    _cleanup_runtime_cache(force=False)
    if worker_error is not None:
        raise worker_error

def _wait_for_preview_workers(timeout_sec: float=300.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        with config.preview_lock:
            workers = [w for w in config.preview_workers if w.is_alive()]
            config.preview_workers.clear()
            config.preview_workers.update(workers)
        if not workers:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        config.worker_queue_service.wait_for_futures(workers, timeout_sec=min(0.2, remaining))

def _export_workspace_key(problem_id: int, workspace_id: int, head_commit: str, export_type: str) -> str:
    return f'{int(problem_id)}:{int(workspace_id)}:{str(head_commit or '').strip()}:{str(export_type or '').strip().lower()}'

def _run_export_create_worker(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_build_id: str, requested_export_type: str) -> None:
    safe_requested_build_id = str(requested_build_id or '').strip()
    safe_export_type = str(requested_export_type or '').strip().lower() or 'icpc'
    details: dict[str, object] = {'status': 'failed', 'build_id': safe_requested_build_id, 'export_type': safe_export_type, 'source_commit': str(head_commit or '').strip(), 'filename': '', 'error': ''}
    worker_error: Exception | None = None
    try:
        if safe_export_type != 'icpc':
            raise ValueError('unsupported package type (ICPC only)')
        safe_head = str(head_commit or '').strip()
        if not safe_head:
            raise ValueError('no committed revision; commit changes first')
        resolved_build_id = safe_requested_build_id
        if not resolved_build_id:
            active_build = _latest_workspace_committed_build(int(problem_id), int(workspace_id), safe_head, ok_only=True)
            if active_build is None:
                resolved_build_id = config.build_service.run_build(problem, user, commit=safe_head, ref=safe_head)
            else:
                resolved_build_id = str(active_build['id'] or '').strip()
        if not resolved_build_id:
            raise RuntimeError('failed to resolve build id for export')
        build_row = config.db.fetch_one('SELECT status,source_commit,source_ref FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [resolved_build_id, int(problem_id), int(workspace_id)])
        if build_row is None:
            raise ValueError(f'build metadata not found: {resolved_build_id}')
        build_status = str(build_row['status'] or 'missing').strip().lower()
        source_commit = str(build_row['source_commit'] or '').strip()
        source_ref = str(build_row['source_ref'] or '').strip()
        details['build_id'] = resolved_build_id
        details['build_status'] = build_status
        details['source_commit'] = source_commit
        details['source_ref'] = source_ref
        if source_commit != safe_head or source_ref != safe_head:
            raise ValueError('package must be generated from current committed revision')
        if build_status != 'ok':
            raise ValueError(f'build status is {build_status}')
        out = config.export_service.create_export(problem, resolved_build_id, safe_export_type)
        details['status'] = 'ok'
        details['filename'] = out.name
    except Exception as exc:
        details['status'] = 'failed'
        details['error'] = str(exc)
        worker_error = exc
    _audit(actor_user_id, problem_id, 'export.create', details)
    _cleanup_runtime_cache(force=False)
    if worker_error is not None:
        raise worker_error

def _start_export_job(problem: str, user: str, *, actor_user_id: int, problem_id: int, workspace_id: int, head_commit: str, requested_build_id: str, requested_export_type: str, initial_details: dict[str, object] | None=None) -> bool:
    key = _export_workspace_key(problem_id, workspace_id, head_commit, requested_export_type)
    with config.export_lock:
        if key in config.export_inflight:
            return False
        config.export_inflight.add(key)
    if isinstance(initial_details, dict):
        try:
            _audit(actor_user_id, problem_id, 'export.create', initial_details)
        except Exception:
            with config.export_lock:
                config.export_inflight.discard(key)
            raise
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_export_create_worker(problem, user, actor_user_id=actor_user_id, problem_id=problem_id, workspace_id=workspace_id, head_commit=head_commit, requested_build_id=requested_build_id, requested_export_type=requested_export_type)
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.export_lock:
                    config.export_workers.discard(worker)
                    config.export_inflight.discard(key)
    thread_name = key.replace(':', '-')
    try:
        worker, queued = config.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            backend=config.sandbox_backend.name,
            dedupe_key=f'export:{key}',
        )
        worker_ref[0] = worker
        if not queued:
            with config.export_lock:
                config.export_inflight.discard(key)
            return False
        with config.export_lock:
            config.export_workers.add(worker)
    except Exception:
        with config.export_lock:
            worker = worker_ref[0]
            if worker is not None:
                config.export_workers.discard(worker)
            config.export_inflight.discard(key)
        raise
    return True

def _wait_for_export_workers(timeout_sec: float=300.0) -> None:
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    while True:
        with config.export_lock:
            workers = [w for w in config.export_workers if w.is_alive()]
            config.export_workers.clear()
            config.export_workers.update(workers)
        if not workers:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        config.worker_queue_service.wait_for_futures(workers, timeout_sec=min(0.2, remaining))

def _checker_standard_from_build_cfg(build_cfg: dict) -> str:
    raw = str(build_cfg.get('checker_standard') or '').strip()
    if not raw:
        return ''
    try:
        return _canonical_standard_checker_name(raw)
    except ValueError:
        return ''

def _component_repo_source_from_build_cfg(workspace: Path, build_cfg: dict, config_key: str, folder: str, default_filename: str) -> tuple[str, bool]:
    configured = _normalize_workspace_rel_path(build_cfg.get(config_key))
    if configured:
        try:
            configured_abs = _safe_workspace_path(workspace, configured)
            if configured_abs.exists() and configured_abs.is_file():
                return (configured, True)
        except HTTPException:
            pass
    default_rel = f'{folder}/{default_filename}'
    try:
        default_abs = _safe_workspace_path(workspace, default_rel)
        if default_abs.exists() and default_abs.is_file():
            return (default_rel, True)
    except HTTPException:
        pass
    folder_path = workspace / folder
    names: list[str] = []
    try:
        if folder_path.exists() and folder_path.is_dir() and (not folder_path.is_symlink()):
            with os.scandir(folder_path) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if not name.endswith('.cpp'):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    names.append(name)
    except OSError:
        names = []
    if not names:
        return (default_rel, False)
    names.sort()
    return (f'{folder}/{names[0]}', True)

def _checker_repo_source_from_build_cfg(workspace: Path, build_cfg: dict) -> tuple[str, bool]:
    return _component_repo_source_from_build_cfg(workspace, build_cfg, 'checker_source', 'checkers', 'checker.cpp')

def _source_basename_label(path: str) -> str:
    raw = str(path or '').strip()
    if not raw:
        return ''
    name = Path(raw).name.strip()
    return name or raw

def _generator_sources_from_build_cfg(build_cfg: dict) -> list[str]:
    sources: list[str] = []
    raw_sources = build_cfg.get('generator_sources')
    if isinstance(raw_sources, list):
        for item in raw_sources:
            normalized = _normalize_optional_component_source_path_safe(item, 'generators', 'generator source')
            if normalized:
                sources.append(normalized)
    return _dedupe_preserve_order(sources)

def _resolve_generator_source_from_token_for_nav(token: str, source_paths: list[str]) -> str:
    raw = str(token or '').strip().replace('\\', '/')
    while raw.startswith('./'):
        raw = raw[2:]
    if not raw:
        return ''
    if any((part == '..' for part in raw.split('/'))):
        return ''
    normalized_sources = _dedupe_preserve_order([str(path or '').strip().replace('\\', '/') for path in source_paths if str(path or '').strip()])
    if not normalized_sources:
        return ''
    source_set = set(normalized_sources)
    token_path = Path(raw)
    suffix = token_path.suffix.lower()
    candidates: list[str] = []
    if raw.startswith('generators/'):
        if suffix in _C.CPP_SOURCE_EXTENSIONS:
            candidates.append(raw)
        else:
            for ext in _C.CPP_SOURCE_EXTENSIONS:
                candidates.append(f'{raw}{ext}')
    elif suffix in _C.CPP_SOURCE_EXTENSIONS:
        candidates.append(f'generators/{raw}')
    else:
        candidates.append(f'generators/{raw}')
        for ext in _C.CPP_SOURCE_EXTENSIONS:
            candidates.append(f'generators/{raw}{ext}')
    seen: set[str] = set()
    for rel in candidates:
        rel_key = str(rel or '').strip()
        if not rel_key or rel_key in seen:
            continue
        seen.add(rel_key)
        if rel_key in source_set:
            return rel_key
    name = token_path.name
    if suffix in _C.CPP_SOURCE_EXTENSIONS:
        exact = [rel for rel in normalized_sources if Path(rel).name == name]
        if len(exact) == 1:
            return exact[0]
    else:
        stem_matches = [rel for rel in normalized_sources if Path(rel).stem == token_path.name]
        if len(stem_matches) == 1:
            return stem_matches[0]
    return ''

def _count_used_configured_generators(workspace: Path, configured_sources: list[str], source_paths: list[str]) -> int:
    configured = _dedupe_preserve_order([str(path or '').strip().replace('\\', '/') for path in configured_sources if str(path or '').strip()])
    if not configured:
        return 0
    configured_set = set(configured)
    source_catalog = _dedupe_preserve_order([*[str(path or '').strip().replace('\\', '/') for path in source_paths if str(path or '').strip()], *configured])
    try:
        entries, _ = _read_tests_spec(workspace)
    except Exception:
        return 0
    used: set[str] = set()
    for row in entries:
        if str(row.get('kind') or '').strip().lower() != 'gen':
            continue
        try:
            command = str(_tests_spec_read_payload(workspace, row) or '').strip()
        except Exception:
            command = ''
        if not command:
            continue
        try:
            tokens = parse_gen_command_tokens(command)
        except Exception:
            continue
        resolved = _resolve_generator_source_from_token_for_nav(tokens[0], source_catalog)
        if resolved and resolved in configured_set:
            used.add(resolved)
    return len(used)

def _generator_status_context(workspace: Path) -> dict:
    build_cfg, _ = _read_build_config(workspace)
    configured_sources = _generator_sources_from_build_cfg(build_cfg)
    generator_candidates, generator_candidates_truncated = _list_cpp_sources(workspace, 'generators')
    repo_source = ''
    repo_exists = False
    for rel in configured_sources:
        if _workspace_rel_file_exists(workspace, rel):
            repo_source = rel
            repo_exists = True
            break
    if not repo_source and configured_sources:
        repo_source = configured_sources[0]
    if not repo_source:
        repo_source = 'generators/generator.cpp'
        repo_exists = _workspace_rel_file_exists(workspace, repo_source)
    else:
        repo_exists = _workspace_rel_file_exists(workspace, repo_source)
    configured_set = set(configured_sources)
    all_sources = _dedupe_preserve_order([*configured_sources, *generator_candidates])
    has_declared_or_discovered = bool(all_sources)
    source_rows: list[dict[str, object]] = []
    for rel in all_sources:
        source_rows.append({'path': rel, 'exists': _workspace_rel_file_exists(workspace, rel), 'configured': rel in configured_set})
    if not source_rows:
        source_rows.append({'path': repo_source, 'exists': bool(repo_exists), 'configured': False})
    if repo_exists:
        mode = 'repository'
        display = _source_basename_label(repo_source)
    elif has_declared_or_discovered:
        mode = 'missing'
        display = 'missing'
    else:
        mode = 'empty'
        display = '0 files'
    return {'mode': mode, 'display': display, 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists), 'configured_sources': source_rows, 'source_rows_truncated': bool(generator_candidates_truncated)}

def _validator_status_context(workspace: Path) -> dict:
    build_cfg, _ = _read_build_config(workspace)
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'validator_source', 'validators', 'validator.cpp')
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def _interactor_status_context(workspace: Path) -> dict:
    build_cfg, _ = _read_build_config(workspace)
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'interactor_source', 'interactors', 'interactor.cpp')
    return {'mode': 'repository' if repo_exists else 'missing', 'display': repo_source if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def _checker_status_context(workspace: Path) -> dict:
    build_cfg, _ = _read_build_config(workspace)
    standard = _checker_standard_from_build_cfg(build_cfg)
    if standard:
        valid = True
        try:
            _resolve_standard_checker_path(standard)
        except ValueError:
            valid = False
        return {'mode': 'standard', 'display': standard, 'standard_checker': standard, 'standard_valid': valid, 'repo_source': '', 'repo_source_exists': False}
    repo_source, repo_exists = _checker_repo_source_from_build_cfg(workspace, build_cfg)
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'standard_checker': '', 'standard_valid': True, 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

def _cap_summary_list(summary: dict, field: str, limit: int, truncated_key: str, total_key: str, limit_key: str) -> None:
    values = summary.get(field)
    if not isinstance(values, list):
        return

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value >= 0 else None
    cap = max(1, int(limit))
    existing_total = _int_or_none(summary.get(total_key))
    existing_truncated = summary.get(truncated_key) if isinstance(summary.get(truncated_key), bool) else None
    total = len(values)
    if existing_total is not None:
        total = max(total, existing_total)
    shown = values
    if len(values) > cap:
        shown = values[:cap]
        summary[field] = shown
    summary[limit_key] = cap
    summary[total_key] = total
    if existing_truncated is not None:
        summary[truncated_key] = bool(existing_truncated) or total > cap or len(values) > cap
        return
    summary[truncated_key] = total > cap

def _cap_run_test_feedback_files(summary: dict, limit: int) -> None:
    tests = summary.get('tests')
    if not isinstance(tests, list):
        return

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value >= 0 else None
    cap = max(1, int(limit))
    for row in tests:
        if not isinstance(row, dict):
            continue
        files = row.get('feedback_files')
        if not isinstance(files, list):
            continue
        existing_total = _int_or_none(row.get('feedback_files_total'))
        existing_truncated = row.get('feedback_files_truncated') if isinstance(row.get('feedback_files_truncated'), bool) else None
        total = len(files)
        if existing_total is not None:
            total = max(total, existing_total)
        if len(files) > cap:
            row['feedback_files'] = files[:cap]
        row['feedback_files_limit'] = cap
        row['feedback_files_total'] = total
        if existing_truncated is not None:
            row['feedback_files_truncated'] = bool(existing_truncated) or total > cap or len(files) > cap
            continue
        row['feedback_files_truncated'] = total > cap

def _truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or '')
    if len(text) <= cap:
        return (text, False)
    return (text[:cap] + f'... [truncated; showing first {cap} characters]', True)

def _normalize_diagnostics(entries: list, message_limit: int) -> list[dict]:

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value > 0 else None
    normalized: list[dict] = []
    for raw in entries:
        item = raw if isinstance(raw, dict) else {'message': str(raw or '')}
        msg, msg_truncated = _truncate_inline_text(str(item.get('message') or ''), message_limit)
        persisted_truncated = bool(item.get('message_truncated')) if isinstance(item, dict) else False
        persisted_limit = _int_or_none(item.get('message_limit')) if isinstance(item, dict) else None
        row = dict(item)
        row['message'] = msg
        row['message_truncated'] = bool(msg_truncated) or persisted_truncated
        if msg_truncated:
            row['message_limit'] = message_limit
        elif persisted_truncated and persisted_limit is not None:
            row['message_limit'] = persisted_limit
        else:
            row['message_limit'] = message_limit
        row.setdefault('level', 'error')
        row.setdefault('file', '')
        row.setdefault('line', 0)
        row.setdefault('column', 0)
        row.setdefault('can_link', False)
        normalized.append(row)
    return normalized

def _diagnostic_file_display(file_path: str) -> str:
    text = str(file_path or '').strip()
    if not text:
        return ''
    normalized = text.replace('\\', '/')
    normalized = re.sub('/run-[A-Za-z0-9._-]+/', '/run/', normalized)
    is_absolute_like = normalized.startswith('/') or bool(re.match('^[A-Za-z]:/', normalized))
    if not is_absolute_like:
        return normalized
    pieces = [part for part in normalized.split('/') if part]
    if not pieces:
        return normalized
    if len(pieces) >= 2:
        return '/'.join(pieces[-2:])
    return pieces[-1]

def _decorate_compile_diagnostics(entries: list[dict]) -> list[dict]:
    decorated: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        file_text = str(row.get('file') or '').strip()
        file_display = _diagnostic_file_display(file_text)
        try:
            line_value = max(0, int(row.get('line') or 0))
        except Exception:
            line_value = 0
        try:
            column_value = max(0, int(row.get('column') or 0))
        except Exception:
            column_value = 0
        location_display = file_display or '(unknown)'
        if line_value > 0:
            location_display += f':{line_value}'
            if column_value > 0:
                location_display += f':{column_value}'
        location_title = location_display
        row['file_display'] = file_display or '(unknown)'
        row['location_display'] = location_display
        row['location_title'] = location_title or location_display
        level = str(row.get('level') or 'error').strip().lower()
        if not level:
            level = 'error'
        row['level'] = level
        row['level_upper'] = level.upper()
        decorated.append(row)
    return decorated

def _coerce_int(raw, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        return default
    return max(min_value, min(max_value, value))

def _form_text(value: object) -> str:
    if isinstance(value, str):
        return value
    default = getattr(value, 'default', '')
    if default is Ellipsis:
        return ''
    if isinstance(default, str):
        return default
    return str(default or '')

def _normalize_problem_mode(raw: object, default: str='pass-fail') -> str:
    token = str(raw or '').strip().lower().replace('_', '-').replace(' ', '-')
    if token in _C.GENERAL_MODE_VALUES:
        return token
    if default in _C.GENERAL_MODE_VALUES:
        return default
    return 'pass-fail'

def _sanitize_stdio_name(raw: str, fallback: str, label: str) -> str:
    value = str(raw or '').strip()
    if not value:
        return fallback
    if len(value) > 128:
        raise ValueError(f'{label} is too long')
    if any((ch.isspace() for ch in value)):
        raise ValueError(f'{label} cannot contain spaces')
    if '/' in value or '\\' in value:
        raise ValueError(f'{label} cannot contain path separators')
    if value in {'.', '..'}:
        raise ValueError(f'{label} is invalid')
    return value

def _read_problem_config(workspace: Path) -> tuple[dict, dict, Path]:
    cfg_path = _safe_workspace_path(workspace, str(_C.GENERAL_CONFIG_REL))
    payload: dict = {}
    if cfg_path.exists() and cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            if isinstance(raw, dict):
                payload = dict(raw)
        except Exception:
            payload = {}
    mode = _normalize_problem_mode(payload.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    ui_cfg = {'input_file': _sanitize_stdio_name(str(payload.get('input_file') or _C.GENERAL_CONFIG_DEFAULTS['input_file']), str(_C.GENERAL_CONFIG_DEFAULTS['input_file']), 'input file'), 'output_file': _sanitize_stdio_name(str(payload.get('output_file') or _C.GENERAL_CONFIG_DEFAULTS['output_file']), str(_C.GENERAL_CONFIG_DEFAULTS['output_file']), 'output file'), 'time_limit_ms': _coerce_int(payload.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS), 'memory_limit_mb': _coerce_int(payload.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB), 'mode': mode}
    return (payload, ui_cfg, cfg_path)

def _render_workspace_page(request: Request, problem: str, user: str, *, show_access_admin: bool=False):
    ctx = page_ctx(problem, user)
    workspace = Path(ctx['workspace']['path'])
    status = config.git_service.status(workspace)
    message = ''
    if show_access_admin:
        acl_entries = _problem_acl_entries(int(ctx['problem']['id']))
        return _template_response(request, 'access.html', {'ctx': ctx, 'message': message, 'acl_entries': acl_entries, 'repo_role_options': ['owner', 'write', 'read']})

    workspace_changes = ctx.get('workspace_changes') if isinstance(ctx, dict) else {}
    change_rows_raw = workspace_changes.get('rows') if isinstance(workspace_changes, dict) else []
    change_rows = [row for row in change_rows_raw if isinstance(row, dict)]
    requested_path = _normalize_workspace_rel_path(request.query_params.get('path'))
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
    return _template_response(request, 'workspace.html', {'ctx': ctx, 'status': status, 'branches': ctx.get('branches', []), 'message': message, 'selected_path': selected_path, 'selected_diff': selected_diff, 'selected_diff_truncated': bool(selected_diff_truncated), 'selected_diff_lines': selected_diff_lines, 'change_rows': change_rows})

def _tests_spec_resolve_index(raw_index: str, total: int) -> int:
    idx = _coerce_int(raw_index, 0, 1, max(1, total))
    if idx < 1 or idx > total:
        raise ValueError('invalid test index')
    return idx

def _parse_manual_batch_items(raw: str) -> list[str]:
    text = str(raw or '').replace('\r\n', '\n').replace('\r', '\n')
    chunks = _C.TESTS_MANUAL_BATCH_SPLIT_RE.split(text)
    items: list[str] = []
    for chunk in chunks:
        body = chunk.strip('\n')
        if not body.strip():
            continue
        items.append(normalize_manual_input(body + '\n'))
    return items

def _parse_gen_batch_items(raw: str) -> list[str]:
    commands: list[str] = []
    for line in str(raw or '').replace('\r\n', '\n').replace('\r', '\n').splitlines():
        cmd = str(line or '').strip()
        if not cmd:
            continue
        commands.append(normalize_gen_command(cmd))
    return commands

def _normalize_run_id_token(raw: str | None) -> str:
    token = str(raw or '').strip()
    if not token:
        return ''
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', token):
        return ''
    return token
def _normalize_run_test_name_token(raw: str | None) -> str:
    token = str(raw or '').strip()
    if not token:
        return ''
    if not _C.RUN_TEST_NAME_RE.fullmatch(token):
        return ''
    return token

def _parse_run_test_names(raw_values: object) -> list[str]:
    values: list[str] = []
    if raw_values is None:
        return values
    if isinstance(raw_values, str):
        values.append(raw_values)
    elif isinstance(raw_values, list):
        values.extend((str(item or '') for item in raw_values))
    elif isinstance(raw_values, tuple):
        values.extend((str(item or '') for item in raw_values))
    else:
        try:
            values.extend((str(item or '') for item in list(raw_values)))
        except Exception:
            values.append(str(raw_values or ''))
    result: list[str] = []
    for raw in values:
        token = _normalize_run_test_name_token(raw)
        if token:
            result.append(token)
    return _dedupe_preserve_order(result)

def _parse_run_detail_ids(request: Request) -> list[str]:
    values: list[str] = []
    for raw in request.query_params.getlist('run_id'):
        token = _normalize_run_id_token(raw)
        if token:
            values.append(token)
    for csv_raw in request.query_params.getlist('run_ids'):
        text = str(csv_raw or '').strip()
        if not text:
            continue
        for part in text.split(','):
            token = _normalize_run_id_token(part)
            if token:
                values.append(token)
    return _dedupe_preserve_order(values)

def _parse_run_detail_invocation_id(request: Request) -> str:
    for raw in request.query_params.getlist('invocation_id'):
        token = _normalize_run_id_token(raw)
        if token:
            return token
    return ''

def _run_source_from_summary(summary: dict | None) -> str:
    if not isinstance(summary, dict):
        return ''
    return str(summary.get('source') or '').strip()

def _run_rejudge_source_context(source: str, workspace: Path) -> tuple[str, str]:
    source_text = str(source or '').strip()
    if not source_text:
        return ('', 'run source missing')
    safe_solution = _normalize_optional_component_source_path_safe(source_text, 'solutions', 'solution path')
    if not safe_solution:
        return ('', 'source is upload or outside solutions/')
    if not _workspace_rel_file_exists(workspace, safe_solution):
        return ('', f'source file missing in current workspace ({safe_solution})')
    return (safe_solution, '')

def _summarize_rejudge_unavailable_reason(reasons: list[str]) -> str:
    unique_reasons = _dedupe_preserve_order([str(item or '').strip() for item in reasons if str(item or '').strip()])
    if not unique_reasons:
        return 'no reusable solutions source'
    if len(unique_reasons) <= 2:
        return '; '.join(unique_reasons)
    hidden = len(unique_reasons) - 2
    return f'{unique_reasons[0]}; {unique_reasons[1]}; +{hidden} more'

def _run_test_count_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    tests = summary.get('tests')
    if isinstance(tests, list):
        return len(tests)
    usage = summary.get('usage')
    if isinstance(usage, dict):
        try:
            return max(0, int(usage.get('tests') or 0))
        except Exception:
            return 0
    return 0

def _run_invocation_block(summary: dict | None) -> dict:
    if not isinstance(summary, dict):
        return {}
    payload = summary.get('invocation')
    if isinstance(payload, dict):
        return payload
    return {}

def _run_invocation_id_from_summary(summary: dict | None, fallback_run_id: str) -> str:
    block = _run_invocation_block(summary)
    invocation_id = _normalize_run_id_token(block.get('id')) if isinstance(block, dict) else ''
    return invocation_id or str(fallback_run_id or '').strip()

def _run_invocation_run_ids_from_summary(summary: dict | None) -> list[str]:
    block = _run_invocation_block(summary)
    run_ids_raw = block.get('run_ids') if isinstance(block, dict) else None
    if not isinstance(run_ids_raw, list):
        return []
    values: list[str] = []
    for raw in run_ids_raw:
        token = _normalize_run_id_token(raw)
        if token:
            values.append(token)
    return _dedupe_preserve_order(values)

def _run_invocation_run_ids_from_audit(problem_id: int, actor_user_id: int, invocation_id: str) -> list[str]:
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return []
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT 240\n        ", [int(problem_id), int(actor_user_id)])
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        if _normalize_run_id_token(details.get('invocation_id')) != safe_invocation_id:
            continue
        run_ids: list[str] = []
        raw_run_ids = details.get('run_ids')
        if isinstance(raw_run_ids, list):
            for item in raw_run_ids:
                token = _normalize_run_id_token(item)
                if token:
                    run_ids.append(token)
        return _dedupe_preserve_order(run_ids)
    return []

def _run_invocation_maps_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> tuple[dict[str, str], dict[str, list[str]]]:
    cap = max(40, int(limit))
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT ?\n        ", [int(problem_id), int(actor_user_id), cap])
    run_to_invocation: dict[str, str] = {}
    invocation_to_runs: dict[str, list[str]] = {}
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        invocation_token = _normalize_run_id_token(details.get('invocation_id'))
        if not invocation_token:
            continue
        candidates: list[str] = []
        primary = _normalize_run_id_token(details.get('run_id'))
        if primary:
            candidates.append(primary)
        raw_run_ids = details.get('run_ids')
        if isinstance(raw_run_ids, list):
            for item in raw_run_ids:
                token = _normalize_run_id_token(item)
                if token:
                    candidates.append(token)
        deduped = _dedupe_preserve_order(candidates)
        if deduped:
            existing = invocation_to_runs.get(invocation_token)
            if isinstance(existing, list) and existing:
                invocation_to_runs[invocation_token] = _dedupe_preserve_order([*existing, *deduped])
            else:
                invocation_to_runs[invocation_token] = deduped
        for run_token in deduped:
            if run_token and run_token not in run_to_invocation:
                run_to_invocation[run_token] = invocation_token
    return (run_to_invocation, invocation_to_runs)

def _run_invocation_run_id_map_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> dict[str, str]:
    mapping, _declared = _run_invocation_maps_from_audit(problem_id, actor_user_id, limit=limit)
    return mapping

def _run_invocation_id_from_run_id_audit(problem_id: int, actor_user_id: int, run_id: str) -> str:
    safe_run_id = _normalize_run_id_token(run_id)
    if not safe_run_id:
        return ''
    mapping = _run_invocation_run_id_map_from_audit(problem_id, actor_user_id, limit=200)
    return str(mapping.get(safe_run_id) or '')

def _run_invocation_scope_run_ids(problem_id: int, workspace_id: int, actor_user_id: int, invocation_id: str) -> list[str]:
    requested_token = _normalize_run_id_token(invocation_id)
    if not requested_token:
        return []
    safe_invocation_id = requested_token
    mapped_invocation_id = ''
    if safe_invocation_id.startswith('r-'):
        mapped_invocation_id = _run_invocation_id_from_run_id_audit(problem_id, actor_user_id, safe_invocation_id)
        if mapped_invocation_id:
            safe_invocation_id = mapped_invocation_id
    scan_limit = max(160, _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS * 8)
    rows = config.db.fetch_all('\n        SELECT id,created_at,length(summary_json) AS summary_len,summary_json\n        FROM runs\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT ?\n        ', [int(problem_id), int(workspace_id), int(scan_limit)])
    member_ids_seen: set[str] = set()
    member_ids_desc: list[str] = []
    declared_run_ids: list[str] = []
    summary_budget_used = 0
    summary_rows_loaded = 0
    for row in rows:
        run_id = _normalize_run_id_token(row['id'])
        if not run_id:
            continue
        summary_obj: dict | None = None
        try:
            summary_len = int(row['summary_len'] or 0)
        except Exception:
            summary_len = 0
        should_load_summary = summary_len > 0 and summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT and (summary_budget_used + summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET) and (summary_rows_loaded < _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS)
        if should_load_summary:
            summary_obj = _parse_summary_json(row['summary_json'], f'run/invocation/{run_id}')
            summary_budget_used += summary_len
            summary_rows_loaded += 1
        current_invocation = _run_invocation_id_from_summary(summary_obj, '')
        if current_invocation != safe_invocation_id:
            continue
        if run_id not in member_ids_seen:
            member_ids_seen.add(run_id)
            member_ids_desc.append(run_id)
        declared_run_ids.extend(_run_invocation_run_ids_from_summary(summary_obj))
    ordered: list[str] = []
    for token in _dedupe_preserve_order(declared_run_ids):
        if token and token not in ordered:
            ordered.append(token)
    for token in reversed(member_ids_desc):
        if token and token not in ordered:
            ordered.append(token)
    if ordered:
        return ordered
    audit_ids = _run_invocation_run_ids_from_audit(problem_id, actor_user_id, safe_invocation_id)
    if audit_ids:
        return audit_ids
    if requested_token.startswith('r-'):
        direct = config.db.fetch_one('SELECT id FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [requested_token, int(problem_id), int(workspace_id)])
        if direct is not None:
            return [requested_token]
    return []

def _upload_compile_check_error(workspace: Path, upload_filename: str, upload_content: bytes) -> str:
    return _upload_compile_check_error_impl(workspace, upload_filename, upload_content, compile_program=config.toolchain_service.compile_program, cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS))

def _workspace_source_compile_check_error(workspace: Path, source_path: str) -> str:
    return _workspace_source_compile_check_error_impl(workspace, source_path, compile_program=config.toolchain_service.compile_program, cxxflags=list(config.run_service.SUBMISSION_CPP_CXXFLAGS))

def _solution_compile_check_error(workspace: Path, submission_path: str) -> str:
    return _workspace_source_compile_check_error(workspace, submission_path)

def _run_expected_behavior_from_summary(summary: dict | None, source: str) -> str:
    block = _run_invocation_block(summary)
    expected = normalize_expected_behavior(str(block.get('expected_behavior') or 'unknown')) if isinstance(block, dict) else 'unknown'
    if expected != 'unknown':
        return expected
    safe_solution = _normalize_optional_component_source_path_safe(source, 'solutions', 'solution path')
    if safe_solution:
        inferred = infer_expected_behavior_from_name(safe_solution)
        if inferred != 'unknown':
            return inferred
    return 'unknown'

def _run_expected_match_for_summary(run_status: str, summary: dict | None, expected_behavior: str) -> tuple[bool, bool, bool, str]:
    block = _run_invocation_block(summary)
    matched_raw = block.get('matched') if isinstance(block, dict) else None
    completed_raw = block.get('completed') if isinstance(block, dict) else None
    observed_pass_raw = block.get('passed_all_tests') if isinstance(block, dict) else None
    reason_raw = block.get('reason') if isinstance(block, dict) else None
    if isinstance(matched_raw, bool):
        completed = bool(completed_raw) if isinstance(completed_raw, bool) else False
        observed_pass = bool(observed_pass_raw) if isinstance(observed_pass_raw, bool) else False
        reason = str(reason_raw or '')
        return (bool(matched_raw), completed, observed_pass, reason)
    return _verification_solution_match(expected_behavior, run_status, summary)

def _effective_run_timeout_ms(time_limit_ms: int) -> int:
    tl = max(1, int(time_limit_ms))
    return max(tl * 2, tl + 1000)

def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    limits = summary.get('limits')
    if isinstance(limits, dict):
        try:
            cpu_ms = int(limits.get('cpu_ms') or 0)
            if cpu_ms > 0:
                return cpu_ms
        except Exception:
            pass
    run_cfg = summary.get('run_config')
    if not isinstance(run_cfg, dict):
        return 0
    try:
        time_limit_ms = int(run_cfg.get('time_limit_ms') or 0)
        if time_limit_ms > 0:
            return _effective_run_timeout_ms(time_limit_ms)
    except Exception:
        pass
    try:
        run_timeout_ms = int(run_cfg.get('run_timeout_ms') or 0)
        if run_timeout_ms > 0:
            return run_timeout_ms
    except Exception:
        pass
    try:
        run_timeout_sec = int(run_cfg.get('run_timeout_sec') or 0)
        if run_timeout_sec > 0:
            return run_timeout_sec * 1000
    except Exception:
        pass
    return 0

def _run_cell_kind(verdict: str, expected_behavior: str) -> str:
    short = _run_verdict_short(verdict)
    if short in {'', '-', '--'}:
        return 'neutral'
    expected_short = _run_expected_short(expected_behavior)
    if expected_short != '--':
        # For non-accepted expected solutions, AC on a single test is not
        # informative and should not be marked as mismatch.
        if short == 'AC' and expected_short != 'AC':
            return 'neutral'
        if short != expected_short:
            return 'fail'
        if short == 'AC':
            return 'ok'
        return 'expected-nonac'
    if short == 'AC':
        return 'ok'
    return 'neutral'

def _run_verdict_short(verdict: str) -> str:
    value = str(verdict or '').strip().upper()
    if value in {'OK', 'AC'}:
        return 'AC'
    if value in {'CE', 'COMPILE_ERROR', 'COMPILE ERROR'}:
        return 'CE'
    if value == 'WA':
        return 'WA'
    if value == 'TL' or value.startswith('TL'):
        return 'TL'
    if value == 'RE':
        return 'RE'
    if value in {'FAIL', 'FAILED', 'FL'}:
        return 'FL'
    if value in {'', '-'}:
        return '--'
    return 'FL'

def _run_error_display(error: str) -> str:
    raw = str(error or '').strip()
    code = raw.lower()
    if code in {'compile_error', 'compile error', 'ce'}:
        return 'CE'
    return raw

def _run_expected_short(expected_behavior: str) -> str:
    safe = normalize_expected_behavior(expected_behavior)
    if safe == 'accepted':
        return 'AC'
    if safe == 'wrong_answer':
        return 'WA'
    if safe == 'time_limit_exceeded':
        return 'TL'
    if safe == 'run_time_error':
        return 'RE'
    if safe == 'failed':
        return 'FL'
    return '--'

def _run_actual_failed_codes(run_status: str, summary: dict | None) -> list[str]:
    status = str(run_status or '').strip().lower()
    if status in {'running', 'queued', 'pending'}:
        return []
    error_code = str(summary.get('error') or '').strip().lower() if isinstance(summary, dict) else ''
    if error_code in {'compile_error', 'compile error', 'ce'}:
        return ['CE']
    tests = summary.get('tests') if isinstance(summary, dict) else None
    verdicts: list[str] = []
    if isinstance(tests, list):
        for row in tests:
            if not isinstance(row, dict):
                continue
            code = _run_verdict_short(str(row.get('verdict') or ''))
            if code in {'', '--', 'AC'}:
                continue
            verdicts.append(code)
    if verdicts:
        priority = {'CE': 0, 'TL': 1, 'RE': 2, 'WA': 3, 'FL': 4}
        ordered = sorted(set(verdicts), key=lambda code: (priority.get(code, 99), str(code)))
        return [str(code) for code in ordered]
    if status == 'ok':
        return []
    return ['FL']

def _run_actual_short(run_status: str, summary: dict | None) -> str:
    failed_codes = _run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return str(failed_codes[0] or 'FL')
    status = str(run_status or '').strip().lower()
    if status in {'running', 'queued', 'pending'}:
        return '--'
    return 'AC'

def _run_actual_display(run_status: str, summary: dict | None) -> str:
    failed_codes = _run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return '/'.join(failed_codes)
    return _run_actual_short(run_status, summary)

def _run_memory_mb_text(memory_kb: int) -> str:
    try:
        kb = max(0, int(memory_kb))
    except Exception:
        kb = 0
    mb = (kb + 1023) // 1024
    return f'{mb}MB'

def _run_test_sort_key(test_name: str) -> tuple[int, int, str]:
    text = str(test_name or '').strip()
    stem = Path(text).stem if text else ''
    match = re.search('\\d+', stem or text)
    if match:
        try:
            return (0, int(match.group(0)), text)
        except Exception:
            pass
    return (1, 0, text)

def _run_test_answer_name(test_name: str) -> str:
    name = str(test_name or '').strip()
    if not name:
        return ''
    stem = Path(name).stem
    if not stem:
        stem = name
    return f'{stem}.ans'

def _run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int=40, actor_user_id: int | None=None) -> list[dict]:
    fetch_limit = max(1, int(limit)) * _C.RUN_INVOCATION_LIST_SCAN_FACTOR
    rows = config.db.fetch_all('\n        SELECT id,build_id,mode,status,created_at,length(summary_json) AS summary_len\n        FROM runs\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT ?\n        ', [int(problem_id), int(workspace_id), fetch_limit])
    summary_budget_used = 0
    summary_rows_loaded = 0
    audit_invocation_map: dict[str, str] = {}
    audit_invocation_runs_map: dict[str, list[str]] = {}
    if actor_user_id is not None:
        try:
            audit_invocation_map, audit_invocation_runs_map = _run_invocation_maps_from_audit(int(problem_id), int(actor_user_id), limit=max(160, fetch_limit * 4))
        except Exception:
            audit_invocation_map = {}
            audit_invocation_runs_map = {}
    groups_order: list[str] = []
    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        run_id = str(row['id'] or '').strip()
        if not run_id:
            continue
        summary: dict | None = None
        try:
            summary_len = int(row['summary_len'] or 0)
        except Exception:
            summary_len = 0
        should_load_summary = summary_len > 0 and summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_ROW_CHAR_LIMIT and (summary_budget_used + summary_len <= _C.RUN_INVOCATION_LIST_SUMMARY_TOTAL_CHAR_BUDGET) and (summary_rows_loaded < _C.RUN_INVOCATION_LIST_SUMMARY_MAX_ROWS)
        if should_load_summary:
            summary_row = config.db.fetch_one('SELECT summary_json FROM runs WHERE id=?', [run_id])
            summary_raw = str(summary_row['summary_json'] or '') if summary_row is not None else ''
            if summary_raw:
                summary = _parse_summary_json(summary_raw, f'run/list/{run_id}')
                summary_budget_used += len(summary_raw)
                summary_rows_loaded += 1
        source = _run_source_from_summary(summary)
        status_text = str(row['status'] or '').strip().lower() or 'unknown'
        invocation_id = _run_invocation_id_from_summary(summary, run_id) or run_id
        mapped_invocation_id = str(audit_invocation_map.get(run_id) or '')
        if mapped_invocation_id:
            invocation_id = mapped_invocation_id
        elif invocation_id == run_id and actor_user_id is not None:
            fallback_invocation_id = _run_invocation_id_from_run_id_audit(problem_id, int(actor_user_id), run_id)
            if fallback_invocation_id:
                invocation_id = fallback_invocation_id
        declared_from_audit = audit_invocation_runs_map.get(invocation_id) or []
        declared_from_summary = _run_invocation_run_ids_from_summary(summary)
        declared_ids = _dedupe_preserve_order([*declared_from_audit, *declared_from_summary])
        if invocation_id not in groups:
            if len(groups_order) >= max(1, int(limit)):
                continue
            groups_order.append(invocation_id)
            groups[invocation_id] = {'id': invocation_id, 'created_at': row['created_at'], 'mode': str(row['mode'] or '').strip(), 'members': [], 'member_ids': set(), 'declared_run_ids': declared_ids}
        group = groups.get(invocation_id)
        if group is None:
            continue
        existing_declared_raw = group.get('declared_run_ids')
        existing_declared = existing_declared_raw if isinstance(existing_declared_raw, list) else []
        group['declared_run_ids'] = _dedupe_preserve_order([*existing_declared, *declared_ids])
        member_ids = group.get('member_ids')
        if isinstance(member_ids, set) and run_id in member_ids:
            continue
        if isinstance(member_ids, set):
            member_ids.add(run_id)
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, reason = _run_expected_match_for_summary(status_text, summary, expected_behavior)
        tests_total = _run_test_count_from_summary(summary)
        rerun_solution, rerun_unavailable_reason = _run_rejudge_source_context(source, workspace)
        if isinstance(group.get('members'), list):
            group['members'].append({'id': run_id, 'source': source, 'status': status_text, 'tests_total': tests_total, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or ''), 'rerun_solution': rerun_solution, 'rerun_unavailable_reason': rerun_unavailable_reason})
    result: list[dict] = []
    for idx, invocation_id in enumerate(groups_order, start=1):
        group = groups.get(invocation_id) or {}
        members_raw = group.get('members')
        members = members_raw if isinstance(members_raw, list) else []
        members_by_id = {str(item.get('id') or ''): item for item in members if isinstance(item, dict)}
        declared_ids_raw = group.get('declared_run_ids')
        declared_ids = declared_ids_raw if isinstance(declared_ids_raw, list) else []
        ordered_member_ids: list[str] = []
        for token in declared_ids:
            run_token = _normalize_run_id_token(token)
            if run_token and run_token not in ordered_member_ids:
                ordered_member_ids.append(run_token)
        for item in members:
            if not isinstance(item, dict):
                continue
            token = _normalize_run_id_token(item.get('id'))
            if token and token not in ordered_member_ids:
                ordered_member_ids.append(token)
        ordered_members: list[dict[str, object]] = []
        for token in ordered_member_ids:
            existing = members_by_id.get(token)
            if isinstance(existing, dict):
                ordered_members.append(existing)
                continue
            ordered_members.append({'id': token, 'source': '', 'status': 'running', 'tests_total': 0, 'expected_behavior': 'unknown', 'expected_behavior_label': expected_behavior_label('unknown'), 'matched': False, 'completed': False, 'passed_all_tests': False, 'reason': 'pending', 'rerun_solution': '', 'rerun_unavailable_reason': 'pending'})
        if not ordered_members:
            continue
        statuses = [str(item.get('status') or '').strip().lower() for item in ordered_members]
        has_running = any((status in {'running', 'queued', 'pending'} for status in statuses))
        matched_count = sum((1 for item in ordered_members if bool(item.get('matched'))))
        total_count = len(ordered_members)
        if has_running:
            status_text = 'running'
        else:
            status_text = 'ok' if total_count > 0 and matched_count == total_count else 'failed'
        is_failed = status_text == 'failed'
        test_totals = [int(item.get('tests_total') or 0) for item in ordered_members if int(item.get('tests_total') or 0) > 0]
        tests_label = 'tests: -'
        tests_total = 0
        if test_totals:
            tests_total = max(test_totals)
            min_total = min(test_totals)
            max_total = max(test_totals)
            if min_total == max_total:
                tests_label = f'tests: 1-{max_total} (all)'
            else:
                tests_label = f'tests: {min_total}-{max_total} (varied)'
        source_items = [str(item.get('source') or '').strip() for item in ordered_members if str(item.get('source') or '').strip()]
        source_display = '-'
        if source_items:
            shown = source_items[:2]
            extra = len(source_items) - len(shown)
            source_display = ', '.join(shown)
            if extra > 0:
                source_display += f', +{extra}'
        rerun_paths = _dedupe_preserve_order([str(item.get('rerun_solution') or '') for item in ordered_members if str(item.get('rerun_solution') or '').strip()])
        rerun_query = '&'.join((f'solution_paths={quote_plus(path)}' for path in rerun_paths))
        rerun_unavailable_reasons = _dedupe_preserve_order([str(item.get('rerun_unavailable_reason') or '') for item in ordered_members if str(item.get('rerun_unavailable_reason') or '').strip()])
        rerun_unavailable_reason = ''
        if not rerun_paths:
            rerun_unavailable_reason = _summarize_rejudge_unavailable_reason(rerun_unavailable_reasons)
        result.append({'index': idx, 'id': invocation_id, 'run_ids': ordered_member_ids, 'run_ids_csv': ','.join(ordered_member_ids), 'run_count': total_count, 'build_id': '', 'mode': str(group.get('mode') or ''), 'status': status_text, 'status_upper': status_text.upper(), 'created_at': group.get('created_at'), 'source_display': source_display, 'tests_label': tests_label, 'tests_total': tests_total, 'matched_count': matched_count, 'is_failed': is_failed, 'rerun_solution_paths': rerun_paths, 'rerun_solution_query': rerun_query, 'rerun_unavailable_reason': rerun_unavailable_reason})
    return result

def _build_run_detail_context(ctx: dict, run_ids: list[str], execute_mode: str, *, allow_latest_fallback: bool=True) -> dict:
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    problem_slug = str(ctx.get('problem', {}).get('slug') or '').strip()
    username = str(ctx.get('user', {}).get('username') or '').strip()
    fallback_timeout_ms = 0
    try:
        _payload, general_cfg, _cfg_path = _read_problem_config(workspace)
        fallback_timeout_ms = _effective_run_timeout_ms(int(general_cfg.get('time_limit_ms') or _C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']))
    except Exception:
        fallback_timeout_ms = 0
    selected_ids = [token for token in run_ids if token]
    if not selected_ids and allow_latest_fallback:
        latest = config.db.fetch_one('SELECT id FROM runs WHERE problem_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 1', [int(ctx['problem']['id']), workspace_id])
        if latest is not None:
            selected_ids = [str(latest['id'] or '').strip()]
    rows_by_id: dict[str, dict] = {}
    if selected_ids:
        placeholders = ','.join(('?' for _ in selected_ids))
        rows = config.db.fetch_all(f'\n            SELECT id,build_id,mode,status,summary_json,created_at,finished_at\n            FROM runs\n            WHERE workspace_id=? AND id IN ({placeholders})\n            ', [workspace_id, *selected_ids])
        for row in rows:
            run_id = str(row['id'] or '').strip()
            if run_id:
                rows_by_id[run_id] = dict(row)
    columns: list[dict] = []
    all_tests: set[str] = set()
    rerun_paths: list[str] = []
    rerun_unavailable_reasons: list[str] = []
    for column_index, run_id in enumerate(selected_ids, start=1):
        row = rows_by_id.get(run_id)
        status = 'running'
        mode = execute_mode
        created_at = now_iso()
        build_id = ''
        summary_raw = None
        if row is not None:
            status = str(row.get('status') or '').strip().lower() or status
            mode = str(row.get('mode') or '').strip() or mode
            created_at = row.get('created_at') or created_at
            build_id = str(row.get('build_id') or '').strip()
            summary_raw = row.get('summary_json')
        summary = _parse_summary_json(summary_raw, f'run/{run_id}') if summary_raw else None
        if isinstance(summary, dict):
            _cap_summary_list(summary, 'tests', _C.RUN_DETAIL_TEST_LIST_LIMIT, 'tests_truncated', 'tests_total', 'tests_limit')
            _cap_summary_list(summary, 'compile_diagnostics', _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT, 'compile_diagnostics_truncated', 'compile_diagnostics_total', 'compile_diagnostics_limit')
            _cap_run_test_feedback_files(summary, _C.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
            compile_diags = summary.get('compile_diagnostics')
            if isinstance(compile_diags, list):
                normalized_diags = _normalize_diagnostics(compile_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        source = _run_source_from_summary(summary)
        title = Path(source).name if source else ''
        if not title:
            title = f'Program {column_index}'
        source_href = ''
        source_rel = _normalize_workspace_rel_path(source)
        if problem_slug and username and source_rel and _workspace_rel_file_exists(workspace, source_rel):
            safe_solution = _normalize_optional_component_source_path_safe(source_rel, 'solutions', 'solution path')
            if safe_solution:
                source_href = f'/problems/{problem_slug}/{username}/solutions/editor?path={quote_plus(safe_solution)}'
            else:
                source_href = f'/problems/{problem_slug}/{username}/files?path={quote_plus(source_rel)}&src=run'
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, match_reason = _run_expected_match_for_summary(status, summary, expected_behavior)
        expected_short = _run_expected_short(expected_behavior)
        got_failed_codes = _run_actual_failed_codes(status, summary)
        got_short = _run_actual_short(status, summary)
        got_display = _run_actual_display(status, summary)
        expected_mismatch = False
        if expected_short != '--' and got_short != '--':
            if expected_short == 'AC':
                expected_mismatch = bool(got_failed_codes)
            elif expected_short == 'FL':
                expected_mismatch = False
            elif got_failed_codes:
                expected_mismatch = any((code != expected_short for code in got_failed_codes))
        safe_solution, rerun_unavailable_reason = _run_rejudge_source_context(source, workspace)
        if safe_solution:
            rerun_paths.append(safe_solution)
        elif rerun_unavailable_reason:
            rerun_unavailable_reasons.append(rerun_unavailable_reason)
        tests_map: dict[str, dict] = {}
        tests_raw = summary.get('tests') if isinstance(summary, dict) else None
        timeout_limit_ms = _run_timeout_ms_from_summary(summary)
        if timeout_limit_ms <= 0:
            timeout_limit_ms = fallback_timeout_ms
        if isinstance(tests_raw, list):
            for idx, item in enumerate(tests_raw, start=1):
                if not isinstance(item, dict):
                    continue
                test_name = str(item.get('test') or idx).strip()
                if not test_name:
                    continue
                verdict = str(item.get('verdict') or '').strip().upper() or '-'
                verdict_short = _run_verdict_short(verdict)
                try:
                    time_ms = int(item.get('time_ms') or 0)
                except Exception:
                    time_ms = 0
                if str(verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (time_ms > timeout_limit_ms):
                    time_ms = timeout_limit_ms
                try:
                    memory_kb = int(item.get('memory_kb') or 0)
                except Exception:
                    memory_kb = 0
                memory_mb_text = _run_memory_mb_text(memory_kb)
                passes_raw = item.get('passes')
                test_stem = Path(test_name).stem
                feedback_display = '-'
                feedback_files_raw = item.get('feedback_files')
                feedback_items: list[str] = []
                if isinstance(feedback_files_raw, list):
                    for feedback_entry in feedback_files_raw:
                        token = str(feedback_entry or '').strip()
                        if token:
                            feedback_items.append(token)
                if feedback_items:
                    feedback_display = ', '.join(feedback_items)
                feedback_total = len(feedback_items)
                try:
                    feedback_total = max(feedback_total, int(item.get('feedback_files_total') or 0))
                except Exception:
                    feedback_total = len(feedback_items)
                feedback_truncated = bool(item.get('feedback_files_truncated'))
                if feedback_total > len(feedback_items):
                    feedback_truncated = True
                if feedback_truncated:
                    hidden_count = max(0, feedback_total - len(feedback_items))
                    if hidden_count > 0:
                        feedback_display = f'{feedback_display} (+{hidden_count} more)' if feedback_display != '-' else f'+{hidden_count} files'
                pass_rows: list[dict[str, str]] = []
                if isinstance(passes_raw, list) and passes_raw:
                    for pass_idx, pass_item in enumerate(passes_raw, start=1):
                        if not isinstance(pass_item, dict):
                            continue
                        pass_no_raw = pass_item.get('pass')
                        try:
                            pass_no = max(1, int(pass_no_raw))
                        except Exception:
                            pass_no = pass_idx
                        pass_verdict = str(pass_item.get('verdict') or '').strip().upper() or '-'
                        pass_verdict_short = _run_verdict_short(pass_verdict)
                        try:
                            pass_time_ms = int(pass_item.get('time_ms') or 0)
                        except Exception:
                            pass_time_ms = 0
                        if str(pass_verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (pass_time_ms > timeout_limit_ms):
                            pass_time_ms = timeout_limit_ms
                        try:
                            pass_memory_kb = int(pass_item.get('memory_kb') or 0)
                        except Exception:
                            pass_memory_kb = 0
                        output_rel = f'{test_stem}.pass{pass_no}.out' if test_stem else ''
                        checker_log_rel = f'feedback_dir/{test_stem}/pass{pass_no}/checker.log' if test_stem else ''
                        pass_rows.append({'pass_label': f'P{pass_no}', 'verdict_short': pass_verdict_short, 'kind': _run_cell_kind(pass_verdict, expected_behavior), 'time_display': f'{pass_time_ms}ms', 'memory_display': _run_memory_mb_text(pass_memory_kb), 'feedback_display': feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel})
                if not pass_rows:
                    output_rel = f'{test_stem}.pass1.out' if test_stem else ''
                    checker_log_rel = f'feedback_dir/{test_stem}/pass1/checker.log' if test_stem else ''
                    pass_rows.append({'pass_label': 'P1', 'verdict_short': verdict_short, 'kind': _run_cell_kind(verdict, expected_behavior), 'time_display': f'{time_ms}ms', 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel})
                all_tests.add(test_name)
                tests_map[test_name] = {'verdict': verdict, 'time_ms': time_ms, 'memory_kb': memory_kb, 'text': verdict_short, 'short': verdict_short, 'metrics': f'{time_ms}ms/{memory_mb_text}', 'kind': _run_cell_kind(verdict, expected_behavior), 'detail': {'verdict': verdict, 'verdict_short': verdict_short, 'time_display': f'{time_ms}ms', 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'pass_rows': pass_rows}}
        columns.append({'id': run_id, 'build_id': build_id, 'title': title, 'source': source or '-', 'source_href': source_href, 'status': status, 'status_upper': status.upper(), 'mode': mode, 'created_at': created_at, 'summary': summary, 'tests_map': tests_map, 'compile_log': str(summary.get('compile_log') or '') if isinstance(summary, dict) else '', 'compile_diagnostics': summary.get('compile_diagnostics') if isinstance(summary, dict) else [], 'compile_diagnostics_truncated': bool(summary.get('compile_diagnostics_truncated')) if isinstance(summary, dict) else False, 'compile_diagnostics_total': int(summary.get('compile_diagnostics_total') or 0) if isinstance(summary, dict) else 0, 'compile_diagnostics_limit': int(summary.get('compile_diagnostics_limit') or 0) if isinstance(summary, dict) else 0, 'error': str(summary.get('error') or '') if isinstance(summary, dict) else '', 'error_display': _run_error_display(str(summary.get('error') or '')) if isinstance(summary, dict) else '', 'tests_total': int(summary.get('tests_total') or len(tests_map)) if isinstance(summary, dict) else len(tests_map), 'tests_truncated': bool(summary.get('tests_truncated')) if isinstance(summary, dict) else False, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_short': expected_short, 'got_short': got_short, 'got_display': got_display, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': str(match_reason or '')})
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    detail_rows: list[dict] = []

    def _build_artifact_preview(build_id: str, rel_path: str) -> dict[str, object]:
        safe_build_id = str(build_id or '').strip()
        safe_rel_path = str(rel_path or '').strip().lstrip('/')
        if not problem_slug or not username or (not safe_rel_path) or (not is_canonical_artifact_id(safe_build_id)):
            return _run_detail_preview_unavailable('missing')
        try:
            preview_file = _safe_artifact_path(problem_slug, safe_build_id, safe_rel_path)
        except HTTPException:
            return _run_detail_preview_unavailable('missing')
        download_href = f'/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/{safe_rel_path}'
        return _run_detail_preview_from_path(preview_file, download_href)

    def _run_artifact_preview(run_id: str, rel_path: str) -> dict[str, object]:
        safe_run_id = _normalize_run_id_token(run_id)
        safe_rel_path = str(rel_path or '').strip().lstrip('/')
        if not problem_slug or not username or (not safe_run_id) or (not safe_rel_path):
            return _run_detail_preview_unavailable('missing')
        try:
            preview_file = _safe_run_artifact_path(ctx, safe_run_id, safe_rel_path)
        except HTTPException:
            return _run_detail_preview_unavailable('missing')
        download_href = f'/problems/{problem_slug}/{username}/runs/{safe_run_id}/artifacts/{safe_rel_path}'
        return _run_detail_preview_from_path(preview_file, download_href)
    for idx, test_name in enumerate(ordered_tests, start=1):
        input_rel = f'tests/{test_name}'
        answer_name = _run_test_answer_name(test_name)
        answer_rel = f'ans/{answer_name}' if answer_name else ''
        input_preview = _run_detail_preview_unavailable('missing')
        answer_preview = _run_detail_preview_unavailable('missing')
        for col in columns:
            build_id = str(col.get('build_id') or '').strip()
            if not is_canonical_artifact_id(build_id):
                continue
            if not bool(input_preview.get('available')):
                input_preview = _build_artifact_preview(build_id, input_rel)
            if answer_rel and (not bool(answer_preview.get('available'))):
                answer_preview = _build_artifact_preview(build_id, answer_rel)
            if bool(input_preview.get('available')) and (not answer_rel or bool(answer_preview.get('available'))):
                break
        cells: list[dict] = []
        for col in columns:
            cell = col['tests_map'].get(test_name)
            if cell is None:
                cells.append({'text': '--', 'short': '--', 'metrics': '-', 'kind': 'neutral', 'detail': None})
                continue
            detail_raw = cell.get('detail') if isinstance(cell.get('detail'), dict) else None
            detail_payload = dict(detail_raw) if isinstance(detail_raw, dict) else None
            if detail_payload is not None:
                pass_rows_payload: list[dict[str, object]] = []
                pass_rows_raw = detail_payload.get('pass_rows')
                if isinstance(pass_rows_raw, list):
                    for pass_item in pass_rows_raw:
                        if not isinstance(pass_item, dict):
                            continue
                        row_payload = dict(pass_item)
                        output_rel = str(row_payload.get('output_rel') or '').strip()
                        output_preview = _run_detail_preview_unavailable('missing')
                        if output_rel:
                            output_preview = _run_artifact_preview(str(col.get('id') or ''), output_rel)
                        row_payload['output_preview'] = output_preview
                        checker_log_rel = str(row_payload.get('checker_log_rel') or '').strip()
                        feedback_preview = _run_detail_preview_unavailable('missing')
                        if checker_log_rel:
                            feedback_preview = _run_artifact_preview(str(col.get('id') or ''), checker_log_rel)
                        row_payload['feedback_preview'] = feedback_preview
                        if str(row_payload.get('feedback_display') or '-').strip() == '-':
                            if bool(feedback_preview.get('available')):
                                preview_text = str(feedback_preview.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                                first_line = ''
                                for raw_line in preview_text.splitlines():
                                    line = str(raw_line or '').strip()
                                    if line:
                                        first_line = line
                                        break
                                if first_line:
                                    if len(first_line) > 160:
                                        first_line = first_line[:157].rstrip() + '...'
                                    row_payload['feedback_display'] = first_line
                        pass_rows_payload.append(row_payload)
                detail_payload['pass_rows'] = pass_rows_payload
            cells.append({'text': str(cell['text']), 'short': str(cell.get('short') or cell.get('text') or '--'), 'metrics': str(cell.get('metrics') or '-'), 'kind': str(cell['kind']), 'detail': detail_payload})
        detail_rows.append({'index': idx, 'test_name': test_name, 'row_id': f'test-detail-{idx}', 'input_preview': input_preview, 'answer_preview': answer_preview, 'cells': cells, 'has_detail': any((cell.get('detail') is not None for cell in cells))})
    return {'detail_columns': columns, 'detail_rows': detail_rows, 'selected_run_ids': selected_ids, 'rerun_solution_paths': _dedupe_preserve_order(rerun_paths), 'rerun_unavailable_reason': _summarize_rejudge_unavailable_reason(rerun_unavailable_reasons) if not rerun_paths else '', 'matched_count': sum((1 for col in columns if bool(col.get('matched')))), 'match_total': len(columns), 'all_matched': bool(columns) and all((bool(col.get('matched')) for col in columns))}
