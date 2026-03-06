from __future__ import annotations
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
from app.impl.auth import _has_sudo_session, _parse_iso_utc, _template_response, _utc_now
from app.impl.config import config
from app.main_utils import (
    _compact_error_text,
    _contains_symlink_component,
    _normalize_optional_component_source_path_safe,
    _normalize_workspace_rel_path,
    _safe_workspace_path,
    _sanitize_log_text_for_ui,
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
from app.services.hashing import quick_fp_digest
from app.services.statement_template import statement_sources_signature
from app.services.tests_spec import (
    TESTS_SPEC_REL,
    dumps_tests_spec,
    load_tests_spec,
    normalize_test_id,
    normalize_test_kind,
    parse_gen_command_tokens,
    payload_rel_path_for_test,
    summarize_tests_spec,
)
from app.services.util import is_canonical_artifact_id, run_cmd

_C = config.constants

_STANDARD_CHECKER_CACHE_TTL_SEC = 2.0
_STANDARD_CHECKER_CACHE_TS = 0.0
_STANDARD_CHECKER_CACHE_AVAILABLE = False
_STANDARD_CHECKER_CACHE_NAMES: tuple[str, ...] = ()
_STANDARD_CHECKER_CACHE_SET: frozenset[str] = frozenset()

_WORKSPACE_ORIGIN_CACHE_TTL_SEC = 10.0
_WORKSPACE_ORIGIN_CACHE: dict[str, tuple[float, Path | None]] = {}
_RUNPIPE_PROTOCOL_TOKEN_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
_EXPECTED_STATUS_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    # Each expected behavior is evaluated by:
    # 1) required: at least one code from this list must appear.
    # 2) allowed: observed codes must be a subset of this list.
    "accepted": {"required": ("AC",), "allowed": ("AC",)},
    "wrong_answer": {"required": ("WA",), "allowed": ("AC", "WA")},
    "tle_or_correct": {"required": (), "allowed": ("AC", "TL")},
    "tle_or_re": {"required": (), "allowed": ("TL", "RE")},
    "time_limit_exceeded": {"required": ("TL",), "allowed": ("AC", "TL")},
    "run_time_error": {"required": ("RE",), "allowed": ("AC", "RE")},
    "rejected": {"required": ("WA", "TL", "RE", "CE"), "allowed": ("AC", "WA", "TL", "RE", "CE")},
    "unknown": {"required": (), "allowed": ("AC", "WA", "TL", "RE", "CE")},
}


def _strip_runpipe_protocol_lines(raw: str) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        if _RUNPIPE_PROTOCOL_TOKEN_RE.search(line):
            continue
        kept.append(line)
    while kept and (not kept[0].strip()):
        kept.pop(0)
    while kept and (not kept[-1].strip()):
        kept.pop()
    return "\n".join(kept)

def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    safe_count = max(0, int(count))
    token = singular if safe_count == 1 else (plural if plural is not None else f'{singular}s')
    return f'{safe_count} {token}'

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
        _payload, general_cfg, _cfg_path = _read_problem_config(workspace_path)
        safe_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        ctx['problem_mode'] = safe_mode
        ctx['general_cfg'] = {'time_limit_ms': _coerce_int(general_cfg.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS), 'memory_limit_mb': _coerce_int(general_cfg.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB), 'mode': safe_mode}
    except Exception:
        ctx['problem_mode'] = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
        ctx['general_cfg'] = {'time_limit_ms': int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), 'memory_limit_mb': int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), 'mode': str(_C.GENERAL_CONFIG_DEFAULTS['mode'])}
    ctx['workspace_revision'] = _workspace_revision_info(
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
        generator_text = f'{_count_label(configured_count, "file")}, {used_count} used'
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
    tests_text = f'{tests_total} ({_count_label(tests_sample, "sample")})' if tests_total > 0 else str(tests_status.get('display') or 'empty') if isinstance(tests_status, dict) else 'empty'
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
    rows = config.db.fetch_all('\n        SELECT p.slug,p.name,a.role AS role,\n               w.id AS workspace_id,w.path,w.branch,w.head_commit,w.dirty,w.updated_at,\n               COALESCE(NULLIF(w.updated_at, \'\'), p.created_at) AS last_updated_at\n        FROM repo_acl a\n        JOIN problems p ON p.id=a.problem_id\n        LEFT JOIN workspaces w ON w.problem_id=p.id AND w.user_id=?\n        WHERE a.user_id=?\n        ORDER BY last_updated_at DESC, p.slug ASC\n        LIMIT ?\n        ', [uid, uid, cap])
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
        items.append({'slug': str(row['slug']), 'name': str(row['name']), 'role': role, 'workspace_id': row['workspace_id'], 'has_workspace': row['workspace_id'] is not None, 'workspace_path': workspace_path_raw, 'branch': branch, 'head_commit': head, 'head_short': head[:8], 'dirty': dirty, 'revision_local': revision['local'], 'revision_upstream': revision['upstream'], 'revision_display': revision['display'], 'revision_highlight': revision['highlight'], 'revision_upstream_higher': revision['upstream_higher'], 'revision_missing': revision['missing'], 'updated_at': row['updated_at'], 'last_updated_at': row['last_updated_at']})
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
    rows = config.db.fetch_all("\n        SELECT c.id,c.slug,c.title,c.owner_user_id,c.created_at,m.role,\n               MAX(\n                   c.created_at,\n                   COALESCE((SELECT MAX(cp0.created_at) FROM contest_problems cp0 WHERE cp0.contest_id=c.id), ''),\n                   COALESCE((SELECT MAX(pr.updated_at) FROM contest_properties pr WHERE pr.contest_id=c.id), ''),\n                   COALESCE((SELECT MAX(cj.created_at) FROM contest_jobs cj WHERE cj.contest_id=c.id), ''),\n                   COALESCE((\n                       SELECT MAX(w2.updated_at)\n                       FROM contest_problems cp2\n                       JOIN workspaces w2 ON w2.problem_id=cp2.problem_id AND w2.user_id=?\n                       WHERE cp2.contest_id=c.id\n                   ), '')\n               ) AS last_updated_at,\n               (\n                   SELECT COUNT(*)\n                   FROM contest_problems cp\n                   WHERE cp.contest_id=c.id\n               ) AS problem_count,\n               (\n                   SELECT group_concat(x.slug, ', ')\n                   FROM (\n                       SELECT p.slug AS slug\n                       FROM contest_problems cp\n                       JOIN problems p ON p.id=cp.problem_id\n                       WHERE cp.contest_id=c.id\n                       ORDER BY p.slug ASC\n                       LIMIT 5\n                   ) x\n               ) AS problem_slugs_preview,\n               (\n                   SELECT COUNT(*)\n                   FROM contest_problems cp3\n                   JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?\n                   WHERE cp3.contest_id=c.id\n                     AND COALESCE(w.dirty, 0) <> 0\n               ) AS dirty_problem_count\n        FROM contests c\n        JOIN contest_members m ON m.contest_id=c.id\n        WHERE m.user_id=?\n        ORDER BY last_updated_at DESC, c.slug ASC\n        LIMIT ?\n        ", [uid, uid, uid, cap])
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
        entries.append({'id': int(row['id']), 'slug': str(row['slug']), 'title': str(row['title']), 'owner_user_id': int(row['owner_user_id']), 'created_at': row['created_at'], 'last_updated_at': row['last_updated_at'], 'role': _normalize_contest_role(row['role']), 'problem_count': problem_count, 'problem_slugs_preview': preview, 'problem_preview_truncated': problem_count > 5, 'dirty_problem_count': dirty_problem_count, 'has_dirty': dirty_problem_count > 0})
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
    key = str(workspace)
    cached = _WORKSPACE_ORIGIN_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and (now - float(cached[0])) <= _WORKSPACE_ORIGIN_CACHE_TTL_SEC:
        return cached[1]
    try:
        proc = run_cmd(['git', '-C', str(workspace), 'remote', 'get-url', 'origin'], timeout=5)
    except Exception:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    if proc.returncode != 0:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    raw = str(proc.stdout or '').strip()
    if not raw:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    remote_path: Path | None = None
    if raw.startswith('file://'):
        parsed = urlparse(raw)
        if parsed.netloc and parsed.netloc not in ('', 'localhost'):
            _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
            return None
        decoded = unquote(parsed.path or '')
        if not decoded:
            _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
            return None
        remote_path = Path(decoded)
    elif '://' in raw:
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    elif ':' in raw and (not raw.startswith('/')) and (not raw.startswith('./')) and (not raw.startswith('../')):
        _WORKSPACE_ORIGIN_CACHE[key] = (now, None)
        return None
    else:
        remote_path = Path(raw)
    if not remote_path.is_absolute():
        remote_path = (workspace / remote_path).resolve()
    else:
        remote_path = remote_path.resolve()
    resolved = remote_path if remote_path.exists() else None
    _WORKSPACE_ORIGIN_CACHE[key] = (now, resolved)
    return resolved

def _workspace_upstream_revision_info(workspace: Path, branch: str) -> tuple[int | None, str | None]:
    upstream_ref = f'origin/{branch}'
    origin_repo = _workspace_origin_local_repo(workspace)
    if origin_repo is not None:
        upstream_branch_ref = f'refs/heads/{branch}'
        version = _git_commit_count(origin_repo, upstream_branch_ref)
        if version is not None:
            return (version, None)
        commit = _git_commit_sha(origin_repo, upstream_branch_ref)
        if commit is not None:
            return (None, commit)
    version = _git_commit_count(workspace, upstream_ref)
    if version is not None:
        return (version, None)
    return (None, _git_commit_sha(workspace, upstream_ref))

def _workspace_revision_info(
    workspace: Path,
    branch: str='main',
    *,
    fetch_remote: bool=False,
    workspace_head: str | None = None,
    workspace_dirty: bool | None = None,
) -> dict:
    safe_branch = str(branch or 'main').strip() or 'main'
    if any((ch.isspace() for ch in safe_branch)):
        safe_branch = 'main'
    safe_head = str(workspace_head or '').strip()
    _ = fetch_remote
    _ = workspace_dirty
    upstream_ref = f'origin/{safe_branch}'
    local_version = _git_commit_count(workspace, 'HEAD')
    local_commit = safe_head or _git_commit_sha(workspace, 'HEAD')
    upstream_version, upstream_commit = _workspace_upstream_revision_info(workspace, safe_branch)
    if local_version is None and local_commit is None:
        local_version = 0
    ahead_count: int | None = None
    behind_count: int | None = None
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
    if ahead_count is None or behind_count is None:
        if local_commit is not None and upstream_commit is not None and (local_commit == upstream_commit):
            ahead_count = 0
            behind_count = 0
        elif local_version == 0 and upstream_version == 0:
            ahead_count = 0
            behind_count = 0
    if upstream_version is None and upstream_commit is None and local_version == 0:
        upstream_version = 0
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
    result = {'local': local_version, 'upstream': upstream_version, 'display': display, 'highlight': highlight, 'upstream_higher': bool(upstream_higher), 'missing': bool(missing), 'ahead_count': ahead_count, 'behind_count': behind_count}
    return result

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
    problem_slug = str(problem or '').strip()
    if not problem_slug:
        raise HTTPException(status_code=404, detail='artifact not found')
    problem_row = config.db.fetch_one('SELECT id FROM problems WHERE slug=?', [problem_slug])
    if problem_row is None:
        raise HTTPException(status_code=404, detail='artifact not found')
    problem_id = int(problem_row['id'])
    row = None
    if aid.startswith('b-'):
        row = config.db.fetch_one(
            'SELECT artifact_path FROM builds WHERE id=? AND problem_id=?',
            [aid, problem_id],
        )
    elif aid.startswith('p-'):
        row = config.db.fetch_one(
            'SELECT artifact_path FROM previews WHERE id=? AND problem_id=?',
            [aid, problem_id],
        )
    else:
        row = config.db.fetch_one(
            '\n            SELECT artifact_path FROM (\n                SELECT artifact_path\n                FROM builds\n                WHERE id=? AND problem_id=?\n                UNION ALL\n                SELECT artifact_path\n                FROM previews\n                WHERE id=? AND problem_id=?\n            )\n            LIMIT 1\n            ',
            [aid, problem_id, aid, problem_id],
        )
    if row is None:
        raise HTTPException(status_code=404, detail='artifact not found')
    artifact_path = str(row['artifact_path'] or '').strip()
    if not artifact_path:
        raise HTTPException(status_code=404, detail='artifact not found')
    try:
        base = config.settings.artifacts_root.resolve()
        root = Path(artifact_path).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail='artifact not found')
    if root != base and base not in root.parents:
        raise HTTPException(status_code=404, detail='artifact not found')
    if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
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
    safe_run_id = str(run_id or '').strip()
    row = config.db.fetch_one('SELECT id FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [safe_run_id, ctx['problem']['id'], ctx['workspace']['id']])
    if row is None:
        raise HTTPException(status_code=404, detail='run not found in workspace')
    try:
        root = config.fs_manager.resolve_run_root(safe_run_id).resolve()
    except Exception:
        raise HTTPException(status_code=404, detail='run artifact directory not found')
    if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
        raise HTTPException(status_code=404, detail='run artifact directory not found')
    return root

def _safe_run_artifact_path(ctx: dict, run_id: str, rel: str) -> Path:
    root = _workspace_run_artifact_root(ctx, run_id)
    norm_rel = rel.lstrip('/')
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
    allowed = {
        'problems',
        'statement',
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
    allowed = {'tests', 'preview', 'statement', 'run', 'export', 'workspace', 'access', 'checker', 'validator', 'interactor', 'solutions', 'generators'}
    return value if value in allowed else ''

def _normalize_source_id(raw: str | None) -> str:
    value = str(raw or '').strip()
    return value if is_canonical_artifact_id(value) else ''

def _files_back_target(problem: str, user: str, source: str, source_id: str) -> tuple[str, str]:
    base = f'/problems/{problem}/{user}'
    if source == 'tests':
        if source_id:
            return (f'{base}/tests?build_id={quote_plus(source_id)}', 'Tests')
        return (f'{base}/tests', 'Tests')
    if source == 'preview':
        if source_id:
            return (f'{base}/preview?preview_id={quote_plus(source_id)}', 'Statement')
        return (f'{base}/preview', 'Statement')
    if source == 'statement':
        return (f'{base}/statement', 'Statement')
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
    for rel in ['config/problem.json', 'statement/problem.tex', 'config/build.json', 'solutions/accepted.cpp']:
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

def _standard_checker_cache_values() -> tuple[tuple[str, ...], frozenset[str], bool]:
    global _STANDARD_CHECKER_CACHE_TS
    global _STANDARD_CHECKER_CACHE_AVAILABLE
    global _STANDARD_CHECKER_CACHE_NAMES
    global _STANDARD_CHECKER_CACHE_SET
    now = time.monotonic()
    if (now - _STANDARD_CHECKER_CACHE_TS) <= _STANDARD_CHECKER_CACHE_TTL_SEC:
        return (_STANDARD_CHECKER_CACHE_NAMES, _STANDARD_CHECKER_CACHE_SET, _STANDARD_CHECKER_CACHE_AVAILABLE)
    root = _C.STANDARD_CHECKER_ROOT
    available = False
    names: list[str] = []
    try:
        if root.exists() and root.is_dir() and (not root.is_symlink()):
            available = True
            with os.scandir(root) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if Path(name).suffix.lower() != '.cpp':
                        continue
                    if not _C.STANDARD_CHECKER_NAME_RE.fullmatch(name):
                        continue
                    try:
                        if entry.is_symlink() or (not entry.is_file(follow_symlinks=False)):
                            continue
                    except OSError:
                        continue
                    names.append(name)
            names.sort()
    except OSError:
        available = False
        names = []
    _STANDARD_CHECKER_CACHE_TS = now
    _STANDARD_CHECKER_CACHE_AVAILABLE = available
    _STANDARD_CHECKER_CACHE_NAMES = tuple(names)
    _STANDARD_CHECKER_CACHE_SET = frozenset(names)
    return (_STANDARD_CHECKER_CACHE_NAMES, _STANDARD_CHECKER_CACHE_SET, _STANDARD_CHECKER_CACHE_AVAILABLE)

def _standard_checker_options() -> list[str]:
    names, _name_set, _available = _standard_checker_cache_values()
    return list(names)

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
    _names, name_set, available = _standard_checker_cache_values()
    if not available:
        raise ValueError('standard checker catalog is unavailable')
    if checker_name not in name_set:
        raise ValueError(f'unknown standard checker: std::{checker_name}')
    source = _C.STANDARD_CHECKER_ROOT / checker_name
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
    normalized = _strip_runpipe_protocol_lines(normalized)
    if not normalized:
        normalized = '(empty)'
    return {'available': True, 'text': normalized, 'truncated': bool(clipped), 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': str(download_href or ''), 'message': ''}


def _run_detail_preview_is_noise(preview: dict[str, object]) -> bool:
    if not isinstance(preview, dict):
        return True
    if not bool(preview.get("available")):
        return True
    text = str(preview.get("text") or "").strip()
    if not text or text == "(empty)":
        return True
    return bool(_RUNPIPE_PROTOCOL_TOKEN_RE.search(text))


def _interactive_transcript_preview(preview: dict[str, object], *, line_limit: int = 24) -> dict[str, object]:
    if not isinstance(preview, dict) or (not bool(preview.get("available"))):
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    raw_text = str(preview.get("text") or "")
    if (not raw_text.strip()) or raw_text.strip() == "(empty)":
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict[str, str]] = []
    last_side = "right"
    for raw_line in lines:
        line = str(raw_line or "").strip()
        if not line:
            continue
        side = ""
        text = line
        if line.startswith("<"):
            side = "left"
            text = line[1:].lstrip()
        elif line.startswith(">"):
            side = "right"
            text = line[1:].lstrip()
        else:
            lower = line.lower()
            if lower.startswith("interactor:") or lower.startswith("jury:") or lower.startswith("judge:"):
                side = "left"
                text = line.split(":", 1)[1].lstrip() if ":" in line else line
            elif lower.startswith("submission:") or lower.startswith("team:") or lower.startswith("solution:"):
                side = "right"
                text = line.split(":", 1)[1].lstrip() if ":" in line else line
        if not side:
            side = "left" if last_side == "right" else "right"
        last_side = side
        rows.append({"side": side, "text": text or line})
    if not rows:
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    cap = max(1, int(line_limit))
    shown_rows = rows[:cap]
    truncated = bool(len(rows) > cap or preview.get("truncated"))
    return {
        "available": True,
        "rows": shown_rows,
        "shown": len(shown_rows),
        "total": len(rows),
        "truncated": truncated,
    }

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
        sample_input = str(entry.get('sample_input') or '')
        sample_output = str(entry.get('sample_output') or '')
        sample_output_validate = _tests_spec_bool_flag(entry.get('sample_output_validate', True))
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
        rows.append(
            {
                'index': idx,
                'id': test_id,
                'kind': kind,
                'sample': sample,
                'sample_input': sample_input,
                'sample_output': sample_output,
                'sample_output_validate': sample_output_validate,
                'custom_sample_input': bool(sample_input),
                'custom_sample_output': bool(sample_output),
                'payload_path': payload_path,
                'payload': payload,
                'preview': preview_text,
                'payload_size_bytes': payload_size_bytes,
                'payload_size_human': _human_size(payload_size_bytes),
                'manual_large_payload': manual_large_payload,
                'preview_bytes_limit': preview_bytes_limit,
                'preview_clipped': preview_clipped,
            }
        )
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
    return {'mode': 'ready', 'display': f'{total} ({_count_label(sample, "sample")})', 'total': total, 'manual': manual, 'gen': gen, 'sample': sample}

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
        count_display = f'{total}+ {"file" if total == 1 else "files"}'
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
        suffix = f" ({'; '.join(parts)})" if parts else ''
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
        suffix = f" ({'; '.join(parts)})" if parts else ''
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
def _verification_sources_signature_from_targets(workspace: Path, file_targets: tuple[str, ...], dir_targets: tuple[str, ...]) -> str:
    entries: list[dict[str, object]] = []
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
        if target is None:
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'missing'})
            return
        try:
            stat_obj = target.stat()
            mtime_ns = int(getattr(stat_obj, 'st_mtime_ns', int(float(stat_obj.st_mtime) * 1_000_000_000)))
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': mtime_ns})
        except OSError:
            entries.append({'kind': 'file', 'target': rel_path, 'state': 'unreadable'})

    def _hash_dir(rel_dir: str) -> None:
        root = workspace / rel_dir
        try:
            if root.is_symlink() or not root.exists() or not root.is_dir():
                entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'missing'})
                return
            root_resolved = root.resolve()
        except OSError:
            entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'missing'})
            return
        if workspace_resolved not in root_resolved.parents and workspace_resolved != root_resolved:
            entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'invalid'})
            return
        entries.append({'kind': 'dir', 'target': rel_dir, 'state': 'ok'})
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
            try:
                stat_obj = path.stat()
                mtime_ns = int(getattr(stat_obj, 'st_mtime_ns', int(float(stat_obj.st_mtime) * 1_000_000_000)))
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'ok', 'size': int(stat_obj.st_size), 'mtime_ns': mtime_ns})
            except OSError:
                entries.append({'kind': 'dir-file', 'target': rel_dir, 'path': rel, 'state': 'unreadable'})

    for rel_path in file_targets:
        _hash_file(rel_path, _safe_file(rel_path))
    for rel_dir in dir_targets:
        _hash_dir(rel_dir)
    return quick_fp_digest(entries, schema='verification-signature.v2')

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


def _expected_status_rule(expected_behavior: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    normalized = normalize_expected_behavior(expected_behavior)
    rule = _EXPECTED_STATUS_RULES.get(normalized, _EXPECTED_STATUS_RULES["unknown"])
    required_codes = tuple(str(code or "").strip().upper() for code in rule.get("required", ()) if str(code or "").strip())
    allowed_codes = tuple(str(code or "").strip().upper() for code in rule.get("allowed", ()) if str(code or "").strip())
    return (normalized, required_codes, allowed_codes)


def _status_codes_display(codes: list[str] | tuple[str, ...]) -> str:
    unique: list[str] = []
    for raw in codes:
        token = str(raw or "").strip().upper()
        if not token or token in unique:
            continue
        unique.append(token)
    return "[" + ", ".join(unique) + "]"


def _status_codes_join(codes: list[str] | tuple[str, ...]) -> str:
    unique: list[str] = []
    for raw in codes:
        token = str(raw or "").strip().upper()
        if not token or token in unique:
            continue
        unique.append(token)
    return "/".join(unique) if unique else "--"


def _status_rule_expected_display(expected_behavior: str) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    return _status_codes_join(required_codes if required_codes else allowed_codes)


def _run_observed_status_codes(run_status: str, summary: dict | None) -> list[str]:
    failed_codes = _run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return [str(code or "").strip().upper() for code in failed_codes if str(code or "").strip()]
    short = _run_actual_short(run_status, summary)
    token = str(short or "").strip().upper()
    if token in {"", "-", "--"}:
        return []
    return [token]


def _status_rule_expectation_display(expected_behavior: str) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}"
    )


def _status_rule_display(expected_behavior: str, run_status: str, summary: dict | None) -> str:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    observed_codes = _run_observed_status_codes(run_status, summary)
    return (
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}, "
        f"got={_status_codes_display(tuple(observed_codes))}"
    )


def _status_rule_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, str]:
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    observed_codes = _run_observed_status_codes(run_status, summary)
    observed_set = set(observed_codes)
    required_set = set(required_codes)
    allowed_set = set(allowed_codes)
    has_required = True if not required_set else bool(observed_set & required_set)
    only_allowed = observed_set.issubset(allowed_set)
    matched = bool(has_required and only_allowed)
    if matched:
        return (True, "")
    reason = _status_rule_display(expected_behavior, run_status, summary)
    return (False, reason)


def _verification_solution_match(expected_behavior: str, run_status: str, summary: dict | None) -> tuple[bool, bool, bool, str]:
    status = str(run_status or '').strip().lower()
    if status in {'running', 'queued', 'pending'}:
        return (False, False, False, 'running')
    completed = _verification_run_completed(run_status, summary)
    observed_pass = _verification_run_passed(run_status, summary)
    rule_matched, rule_reason = _status_rule_match(expected_behavior, run_status, summary)
    matched = bool(completed and rule_matched)
    if matched:
        return (True, True, observed_pass, "")
    if rule_reason:
        return (False, completed, observed_pass, rule_reason)
    if not completed:
        return (False, completed, observed_pass, "solution run did not complete")
    return (False, completed, observed_pass, "verification mismatch")

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
    cancel_reason = ''
    cancel_created_at = ''
    cancel_rows = config.db.fetch_all(
        """
        SELECT details_json,created_at
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='run.cancel'
        ORDER BY created_at DESC
        LIMIT 240
        """,
        [int(problem_id), int(actor_user_id)],
    )
    for cancel_row in cancel_rows:
        cancel_details: dict = {}
        try:
            cancel_payload = json.loads(str(cancel_row['details_json'] or '{}'))
            if isinstance(cancel_payload, dict):
                cancel_details = cancel_payload
        except Exception:
            cancel_details = {}
        cancel_invocation_id = _normalize_run_id_token(cancel_details.get('invocation_id'))
        if not cancel_invocation_id:
            continue
        if cancel_invocation_id == _normalize_run_id_token(details.get('invocation_id')):
            cancel_reason = str(cancel_details.get('reason') or '').strip() or 'verification cancelled by user'
            cancel_created_at = str(cancel_row['created_at'] or '').strip()
            break
    verification_created_at = str(row['created_at'] or '').strip()
    cancelled_after_start = False
    if cancel_created_at:
        cancel_ts = _parse_iso_utc(cancel_created_at)
        verification_ts = _parse_iso_utc(verification_created_at)
        if verification_ts is None:
            cancelled_after_start = True
        elif cancel_ts is not None:
            cancelled_after_start = cancel_ts >= verification_ts
        else:
            cancelled_after_start = True
    if cancelled_after_start:
        details['status'] = 'failed'
        if cancel_reason:
            details['error'] = cancel_reason
        last_status = 'failed'
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
    if cancelled_after_start:
        mode = 'failed'
        stale = False
    stale_reason = _verification_stale_reason(changed_components, head_changed=head_changed, dirty_changed=dirty_changed) if stale else ''
    created_at_value = cancel_created_at if cancelled_after_start and cancel_created_at else verification_created_at
    if cancelled_after_start and cancel_reason:
        error_text = cancel_reason
    return {'mode': mode, 'display': mode, 'last_status': last_status, 'run_id': run_id, 'run_ids': ','.join(run_ids), 'build_id': str(details.get('build_id') or '').strip(), 'error': error_text, 'created_at': created_at_value, 'stale': stale, 'stale_reason': stale_reason}

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
        latest_commit_upper = latest_commit.upper()
        same_ref = bool(latest_id) and (latest_ref == branch)
        matches_head = bool(latest_commit) and (
            latest_commit == head_commit or ((not head_commit) and (latest_commit_upper == 'HEAD'))
        )
        if same_ref and (not dirty) and matches_head:
            return (latest_id, False)
        # Dirty v0 workspaces store build.source_commit as "HEAD".
        if same_ref and dirty and (matches_head or (latest_commit_upper == 'HEAD')):
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

def _parse_build_failure_context(problem_id: int, workspace_id: int, build_id: str) -> tuple[str, str]:
    safe_build_id = str(build_id or '').strip()
    if not safe_build_id:
        return ('', '')
    row = config.db.fetch_one('SELECT status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?', [safe_build_id, int(problem_id), int(workspace_id)])
    if row is None:
        return ('', '')
    status = str(row['status'] or '').strip().lower()
    summary_obj: dict = {}
    summary_raw = str(row['summary_json'] or '')
    if summary_raw:
        try:
            parsed = json.loads(summary_raw)
            if isinstance(parsed, dict):
                summary_obj = parsed
        except Exception:
            summary_obj = {}
    failed_test_raw = str(summary_obj.get('failed_test') or '').strip()
    failed_test = ''
    if failed_test_raw:
        candidate = Path(failed_test_raw).name
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', candidate):
            failed_test = candidate
    failed_step = str(summary_obj.get('failed_step') or '').strip()
    build_error = _compact_error_text(str(summary_obj.get('error') or ''))
    reason = ''
    if build_error:
        reason = build_error
    elif failed_step and failed_test_raw:
        reason = _compact_error_text(f'{failed_step} failed on {failed_test_raw}')
    elif failed_step:
        reason = _compact_error_text(f'{failed_step} failed')
    elif status and status != 'ok':
        reason = f'build status is {status}'
    return (failed_test, reason)

def _extract_failed_test_name_from_error(error_text: str) -> str:
    raw = str(error_text or '')
    match = re.search(r'([A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in)', raw)
    if not match:
        return ''
    token = Path(str(match.group(1) or '')).name
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token):
        return token
    return ''

def _synthesize_failed_run_tests(*, preferred_test: str='', error_text: str='') -> list[dict]:
    test_name = ''
    for raw in [preferred_test, '001.in']:
        token = Path(str(raw or '').strip()).name
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in', token):
            test_name = token
            break
    if not test_name:
        return []
    feedback = _compact_error_text(str(error_text or ''))
    pass_row: dict[str, object] = {'pass': 1, 'verdict': 'FL', 'time_ms': 0, 'memory_kb': 0}
    if feedback:
        pass_row['feedback'] = feedback
    test_row: dict[str, object] = {'test': test_name, 'passes': [pass_row], 'verdict': 'FL', 'sandbox_status': 'fail', 'time_ms': 0, 'memory_kb': 0, 'feedback_files': []}
    if feedback:
        test_row['message'] = feedback
    return [test_row]

def _record_async_run_failure(
    problem: str,
    user: str,
    run_id: str,
    *,
    mode: str,
    source_label: str,
    error: str,
    build_id: str,
    invocation_id: str='',
    invocation_run_ids: list[str] | None=None,
    expected_behavior: str='unknown',
    invocation_source: str='run.execute',
    synthesize_failed_tests: bool=True,
    failure_stage: str='',
    execution_skipped: bool=False,
) -> None:
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
    run_root = config.fs_manager.prepare_run_root(safe_run_id)
    compile_log_name = 'compile.log'
    (run_root / compile_log_name).write_text(safe_error + '\n', encoding='utf-8')
    failed_test, build_reason = _parse_build_failure_context(int(ctx['problem']['id']), int(ctx['workspace']['id']), build_id)
    if not failed_test:
        failed_test = _extract_failed_test_name_from_error(build_reason or safe_error)
    failure_reason = build_reason or safe_error
    safe_failure_stage = str(failure_stage or '').strip().lower()
    tests_payload = _synthesize_failed_run_tests(preferred_test=failed_test, error_text=failure_reason) if bool(synthesize_failed_tests) else []
    summary = {
        'error': safe_error,
        'mode': safe_mode,
        'source': safe_source,
        'tests': tests_payload,
        'tests_total': len(tests_payload),
        'compile_log': compile_log_name,
        'compile_diagnostics': [],
        'toolchain_digest': 'unknown',
        'sandbox_backend': config.sandbox_backend.name,
        'invocation_backend': config.invocation_backend_service.active_backend_name(),
        'limits': {},
        'usage': {'tests': len(tests_payload)},
    }
    if safe_failure_stage:
        summary['failure_stage'] = safe_failure_stage
    if execution_skipped:
        summary['execution_skipped'] = True
        if failure_reason:
            summary['execution_skipped_reason'] = failure_reason
        if not safe_failure_stage:
            summary['failure_stage'] = 'build'
    if safe_invocation_id:
        matched, completed, observed_pass, reason = _verification_solution_match(safe_expected, 'failed', summary)
        invocation_block: dict[str, object] = {'id': safe_invocation_id, 'source': str(invocation_source or 'run.execute').strip() or 'run.execute', 'run_ids': safe_invocation_run_ids, 'expected_behavior': safe_expected, 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or '')}
        if execution_skipped:
            invocation_block['execution_skipped'] = True
        summary['invocation'] = invocation_block
    (run_root / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    now = now_iso()
    existing = config.db.fetch_one('SELECT id FROM runs WHERE id=?', [safe_run_id])
    safe_build_id = str(build_id or '').strip() or _C.RUN_PLACEHOLDER_BUILD_ID
    build_ref = ''
    build_row = config.db.fetch_one(
        'SELECT build_ref FROM builds WHERE id=? AND problem_id=? AND workspace_id=?',
        [safe_build_id, int(ctx['problem']['id']), int(ctx['workspace']['id'])],
    )
    if build_row is not None:
        build_ref = str(build_row['build_ref'] or '').strip().lower()
    if existing is None:
        config.db.execute('\n            INSERT INTO runs(\n                id,problem_id,workspace_id,build_id,build_ref,mode,status,summary_json,artifact_path,created_at,finished_at\n            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)\n            ', [safe_run_id, int(ctx['problem']['id']), int(ctx['workspace']['id']), safe_build_id, build_ref, safe_mode, 'failed', json.dumps(summary), str(run_root), now, now])
        return
    config.db.execute('\n        UPDATE runs\n        SET build_id=?,build_ref=?,mode=?,status=?,summary_json=?,artifact_path=?,finished_at=?\n        WHERE id=?\n        ', [safe_build_id, build_ref, safe_mode, 'failed', json.dumps(summary), str(run_root), now, safe_run_id])

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

def _run_marked_cancelled(problem_id: int, workspace_id: int, run_id: str) -> bool:
    safe_run_id = str(run_id or '').strip()
    if not safe_run_id:
        return False
    row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [safe_run_id, int(problem_id), int(workspace_id)])
    if row is None:
        return False
    status = str(row['status'] or '').strip().lower()
    if status != 'failed':
        return False
    summary_obj = _parse_summary_json(row['summary_json'], f'cancel/{safe_run_id}')
    if not isinstance(summary_obj, dict):
        return False
    if bool(summary_obj.get('cancelled')):
        return True
    error_text = str(summary_obj.get('error') or '').strip().lower()
    return 'cancelled by user' in error_text


def _invocation_marked_cancelled(problem_id: int, actor_user_id: int, invocation_id: str, *, limit: int = 240) -> bool:
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return False
    rows = config.db.fetch_all(
        """
        SELECT details_json
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='run.cancel'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), max(40, int(limit))],
    )
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        if _normalize_run_id_token(details.get('invocation_id')) == safe_invocation_id:
            return True
    return False

def _invocation_submission_parallelism(target_count: int) -> int:
    safe_total = max(0, int(target_count))
    if safe_total <= 1:
        return 1
    backend_name = str(config.invocation_backend_service.active_backend_name() or '').strip().lower()
    if backend_name != 'domjudge-judgehost':
        return 1
    host_count = 0
    fetch_batch_size = 1
    try:
        status = config.judgehost_task_service.status()
    except Exception:
        status = {}
    if isinstance(status, dict):
        try:
            hosts_online = max(0, int(status.get('hosts_online') or 0))
        except Exception:
            hosts_online = 0
        try:
            hosts_total = max(0, int(status.get('hosts_total') or 0))
        except Exception:
            hosts_total = 0
        host_count = hosts_online if hosts_online > 0 else hosts_total
        try:
            fetch_batch_size = max(1, int(status.get('fetch_batch_size') or 1))
        except Exception:
            fetch_batch_size = 1
    if host_count <= 0:
        host_count = 1
    estimate = max(1, host_count * fetch_batch_size)
    return max(1, min(safe_total, 32, estimate))

def _run_execute_batch_worker(
    problem: str,
    user: str,
    *,
    requested_build_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    invocation_id: str,
    invocation_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> None:
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
            _record_async_run_failure(
                problem,
                user,
                str(target.get('run_id') or ''),
                mode=run_mode,
                source_label=str(target.get('source_label') or ''),
                error=err,
                build_id=failed_build_id,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                synthesize_failed_tests=False,
                failure_stage='build',
                execution_skipped=True,
            )
        return
    parallelism = _invocation_submission_parallelism(len(targets))

    def _prepare_target_submission(target: dict[str, object]) -> tuple[dict[str, object], dict[str, object]] | None:
        run_id = str(target.get('run_id') or '').strip()
        if not run_id:
            return None
        if _run_marked_cancelled(problem_id, workspace_id, run_id):
            return None
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
        meta: dict[str, object] = {
            'run_id': run_id,
            'source_label': source_label,
            'expected_behavior': expected_behavior,
        }
        submission_kwargs: dict[str, object] = {
            'problem': problem,
            'username': user,
            'build_id': resolved_build_id,
            'submission_path': submission_path_arg,
            'mode': run_mode,
            'upload_content': upload_content,
            'upload_filename': upload_filename,
            'run_id': run_id,
            'invocation_id': invocation_id,
            'invocation_run_ids': invocation_run_ids,
            'expected_behavior': expected_behavior,
            'invocation_source': 'run.execute',
        }
        if force_recompile:
            submission_kwargs['force_recompile'] = True
        if selected_test_names:
            submission_kwargs['selected_tests'] = selected_test_names
        return (meta, submission_kwargs)

    def _handle_submission_outcome(meta: dict[str, object], *, returned_run_id: str='', error: Exception | None=None) -> None:
        run_id = str(meta.get('run_id') or '').strip()
        if not run_id:
            return
        source_label = str(meta.get('source_label') or '').strip() or 'upload'
        expected_behavior = normalize_expected_behavior(str(meta.get('expected_behavior') or 'unknown'))
        if error is None:
            annotate_run_id = _normalize_run_id_token(returned_run_id) or run_id
            _annotate_run_invocation_result(
                problem_id,
                workspace_id,
                annotate_run_id,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
                expected_behavior=expected_behavior,
                invocation_source='run.execute',
            )
            return
        if _run_marked_cancelled(problem_id, workspace_id, run_id):
            return
        _record_async_run_failure(
            problem,
            user,
            run_id,
            mode=run_mode,
            source_label=source_label,
            error=str(error),
            build_id=resolved_build_id,
            invocation_id=invocation_id,
            invocation_run_ids=invocation_run_ids,
            expected_behavior=expected_behavior,
        )

    if parallelism <= 1:
        for target in targets:
            prepared = _prepare_target_submission(target)
            if prepared is None:
                continue
            meta, submission_kwargs = prepared
            try:
                returned_run_id = str(config.invocation_backend_service.run_submission(**submission_kwargs) or '').strip()
                _handle_submission_outcome(meta, returned_run_id=returned_run_id, error=None)
            except Exception as exc:
                _handle_submission_outcome(meta, error=exc)
    else:
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            inflight: dict[object, dict[str, object]] = {}
            next_index = 0

            def _pump_submit() -> None:
                nonlocal next_index
                while next_index < len(targets) and len(inflight) < parallelism:
                    prepared = _prepare_target_submission(targets[next_index])
                    next_index += 1
                    if prepared is None:
                        continue
                    meta, submission_kwargs = prepared
                    future = pool.submit(config.invocation_backend_service.run_submission, **submission_kwargs)
                    inflight[future] = meta

            _pump_submit()
            while inflight:
                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    meta = inflight.pop(future)
                    try:
                        returned_run_id = str(future.result() or '').strip()
                        _handle_submission_outcome(meta, returned_run_id=returned_run_id, error=None)
                    except Exception as exc:
                        _handle_submission_outcome(meta, error=exc)
                _pump_submit()

def _start_run_execute_batch(
    problem: str,
    user: str,
    *,
    requested_build_id: str,
    run_mode: str,
    targets: list[dict[str, object]],
    invocation_id: str,
    invocation_run_ids: list[str],
    selected_test_names: list[str],
    force_recompile: bool = False,
) -> bool:
    batch_id = str(invocation_id or targets[0].get('run_id') or 'invocation').strip() if targets else 'invocation'
    worker_ref: list[object] = [None]

    def _runner() -> None:
        try:
            _run_execute_batch_worker(
                problem=problem,
                user=user,
                requested_build_id=requested_build_id,
                run_mode=run_mode,
                targets=targets,
                invocation_id=invocation_id,
                invocation_run_ids=invocation_run_ids,
                selected_test_names=selected_test_names,
                force_recompile=bool(force_recompile),
            )
        finally:
            worker = worker_ref[0]
            if worker is not None:
                with config.run_execute_lock:
                    config.run_execute_workers.discard(worker)
    worker, queued, _submit_reason = config.worker_queue_service.submit(
        name=f'run-execute-{batch_id}',
        fn=_runner,
        queue_name='invocation',
        backend=config.invocation_backend_service.active_backend_name(),
        job_type='run',
    )
    worker_ref[0] = worker
    if queued:
        with config.run_execute_lock:
            config.run_execute_workers.add(worker)
    return bool(queued)

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
    build_id = _C.RUN_PLACEHOLDER_BUILD_ID
    verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    ws_row = config.db.fetch_one('SELECT path FROM workspaces WHERE id=? AND problem_id=?', [int(workspace_id), int(problem_id)])
    if ws_row is not None:
        try:
            workspace_path = Path(str(ws_row['path'] or '')).resolve()
            _payload, general_cfg, _cfg_path = _read_problem_config(workspace_path)
            verification_mode = _normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        except Exception:
            verification_mode = str(_C.GENERAL_CONFIG_DEFAULTS['mode'])
    verification_details: dict[str, object] = {'status': 'failed', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'submission_paths': [str(item.get('path') or '') for item in targets], 'solution_count': len(targets), 'invocation_id': invocation_id, 'run_id': run_id, 'run_ids': list(run_ids), 'run_count': len(run_ids), 'invocation_backend': config.invocation_backend_service.active_backend_name(), 'error': ''}
    verification_details['mode'] = verification_mode
    if verification_signature:
        verification_details['verification_signature'] = verification_signature
    if isinstance(verification_signature_details, dict) and verification_signature_details:
        verification_details['verification_signature_details'] = dict(verification_signature_details)

    def _backfill_missing_verification_runs(
        error_text: str,
        *,
        build_for_failure: str,
        execution_skipped_for_missing: bool=False,
    ) -> None:
        safe_error = str(error_text or '').strip() or 'verification failed'
        safe_build_for_failure = str(build_for_failure or _C.RUN_PLACEHOLDER_BUILD_ID).strip() or _C.RUN_PLACEHOLDER_BUILD_ID
        for target in targets:
            token = _normalize_run_id_token(target.get('run_id'))
            if not token:
                continue
            if token not in run_ids:
                run_ids.append(token)
            existing = config.db.fetch_one(
                'SELECT id FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                [token, int(problem_id), int(workspace_id)],
            )
            if existing is not None:
                continue
            _record_async_run_failure(
                problem,
                user,
                token,
                mode=verification_mode,
                source_label=str(target.get('path') or ''),
                error=safe_error,
                build_id=safe_build_for_failure,
                invocation_id=invocation_id,
                invocation_run_ids=run_ids,
                expected_behavior=str(target.get('expected_behavior') or 'unknown'),
                invocation_source='verification.start',
                synthesize_failed_tests=not bool(execution_skipped_for_missing),
                failure_stage='build' if execution_skipped_for_missing else '',
                execution_skipped=bool(execution_skipped_for_missing),
            )
        deduped = _dedupe_preserve_order(run_ids)
        run_ids.clear()
        run_ids.extend(deduped)

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
        target_specs: list[dict[str, object]] = []
        for index, target in enumerate(targets):
            target_specs.append(
                {
                    'index': int(index),
                    'target': target,
                    'source_path': str(target.get('path') or '').strip(),
                    'expected_behavior': normalize_expected_behavior(str(target.get('expected_behavior') or 'unknown')),
                    'requested_run_id': _normalize_run_id_token(target.get('run_id')),
                }
            )
        solution_results_by_index: dict[int, dict[str, object]] = {}
        cancel_reason = 'verification cancelled by user'
        cancel_requested = False

        def _invocation_cancel_requested() -> bool:
            nonlocal cancel_requested
            if cancel_requested:
                return True
            if _invocation_marked_cancelled(problem_id, actor_user_id, invocation_id):
                cancel_requested = True
            return cancel_requested

        parallelism = _invocation_submission_parallelism(len(target_specs))
        active_backend_name = str(config.invocation_backend_service.active_backend_name() or '').strip().lower()
        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            inflight: dict[object, dict[str, object]] = {}
            next_index = 0

            def _store_target_result(
                spec: dict[str, object],
                *,
                current_run_id: str,
                run_row: dict[str, object] | None,
                summary_obj: dict | None,
            ) -> None:
                source_path = str(spec.get('source_path') or '').strip()
                expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                run_status = str(run_row['status'] or 'missing').strip().lower() if run_row is not None else 'missing'
                if run_status in {'queued', 'pending'} and (not isinstance(summary_obj, dict)):
                    summary_obj = {}
                matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, run_status, summary_obj)
                error_text = str(summary_obj.get('error') or '') if isinstance(summary_obj, dict) else ''
                spec_index = int(spec.get('index') or 0)
                solution_results_by_index[spec_index] = {
                    'source_path': source_path,
                    'expected_behavior': expected_behavior,
                    'run_id': current_run_id,
                    'run_status': run_status,
                    'completed': completed,
                    'passed_all_tests': observed_pass,
                    'matched': matched,
                    'reason': reason,
                    'error': error_text,
                }

            def _pump_submit() -> None:
                nonlocal next_index, run_ids
                while next_index < len(target_specs) and len(inflight) < max(1, parallelism):
                    spec = target_specs[next_index]
                    next_index += 1
                    source_path = str(spec.get('source_path') or '').strip()
                    expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                    requested_run_id = _normalize_run_id_token(spec.get('requested_run_id'))
                    target_ref = spec.get('target')
                    if _invocation_cancel_requested():
                        cancel_run_id = requested_run_id or _allocate_run_id()
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = cancel_run_id
                        if cancel_run_id not in run_ids:
                            run_ids.append(cancel_run_id)
                        run_ids = _dedupe_preserve_order(run_ids)
                        run_row = config.db.fetch_one(
                            'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                            [cancel_run_id, problem_id, workspace_id],
                        )
                        if not _run_marked_cancelled(problem_id, workspace_id, cancel_run_id):
                            _record_async_run_failure(
                                problem,
                                user,
                                cancel_run_id,
                                mode=verification_mode,
                                source_label=source_path,
                                error=cancel_reason,
                                build_id=build_id,
                                invocation_id=invocation_id,
                                invocation_run_ids=run_ids,
                                expected_behavior=expected_behavior,
                                invocation_source='verification.start',
                                synthesize_failed_tests=False,
                                failure_stage='cancel',
                                execution_skipped=True,
                            )
                            run_row = config.db.fetch_one(
                                'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                                [cancel_run_id, problem_id, workspace_id],
                            )
                        summary_obj = _parse_summary_json(
                            run_row['summary_json'] if run_row is not None else None,
                            f'verification/{cancel_run_id}',
                        )
                        _store_target_result(spec, current_run_id=cancel_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    if requested_run_id and _run_marked_cancelled(problem_id, workspace_id, requested_run_id):
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = requested_run_id
                        if requested_run_id not in run_ids:
                            run_ids.append(requested_run_id)
                        run_ids = _dedupe_preserve_order(run_ids)
                        run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [requested_run_id, problem_id, workspace_id])
                        summary_obj = _parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{requested_run_id}')
                        _store_target_result(spec, current_run_id=requested_run_id, run_row=run_row, summary_obj=summary_obj)
                        continue
                    submission_run_id = requested_run_id or _allocate_run_id()
                    if isinstance(target_ref, dict):
                        target_ref['run_id'] = submission_run_id
                    if submission_run_id and submission_run_id not in run_ids:
                        run_ids.append(submission_run_id)
                    run_ids = _dedupe_preserve_order(run_ids)
                    invocation_run_ids_snapshot = list(run_ids)
                    prepared_payload: dict[str, object] | None = None
                    if active_backend_name == 'domjudge-judgehost':
                        prepared_payload = config.judgehost_task_service.prepare_enqueue_payload(
                            problem=problem,
                            username=user,
                            build_id=build_id,
                            mode=verification_mode,
                            submission_path=source_path,
                            upload_content=None,
                            upload_filename=None,
                            run_id=submission_run_id,
                            selected_tests=[],
                            invocation_id=invocation_id,
                            invocation_run_ids=invocation_run_ids_snapshot,
                            expected_behavior=expected_behavior,
                            invocation_source='verification.start',
                        )
                    future = pool.submit(
                        config.invocation_backend_service.run_submission,
                        problem=problem,
                        username=user,
                        build_id=build_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=submission_run_id,
                        invocation_id=invocation_id,
                        invocation_run_ids=invocation_run_ids_snapshot,
                        expected_behavior=expected_behavior,
                        invocation_source='verification.start',
                        prepared_payload=prepared_payload,
                    )
                    inflight[future] = spec

            _pump_submit()
            while inflight:
                done, _ = wait(set(inflight.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    spec = inflight.pop(future)
                    source_path = str(spec.get('source_path') or '').strip()
                    expected_behavior = normalize_expected_behavior(str(spec.get('expected_behavior') or 'unknown'))
                    requested_run_id = _normalize_run_id_token(spec.get('requested_run_id'))
                    target_ref = spec.get('target')
                    current_run_id = requested_run_id or ''
                    run_row = None
                    summary_obj: dict | None = None
                    try:
                        current_run_id = str(future.result() or '').strip()
                        current_run_id = _normalize_run_id_token(current_run_id) or current_run_id
                        if requested_run_id and current_run_id and requested_run_id != current_run_id:
                            run_ids = [current_run_id if token == requested_run_id else token for token in run_ids]
                        if current_run_id and current_run_id not in run_ids:
                            run_ids.append(current_run_id)
                        run_ids = _dedupe_preserve_order(run_ids)
                        if isinstance(target_ref, dict):
                            target_ref['run_id'] = current_run_id
                        run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [current_run_id, problem_id, workspace_id])
                        if run_row is None:
                            raise RuntimeError('run metadata missing after submission')
                        summary_obj = _parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{current_run_id}')
                    except Exception as target_exc:
                        fallback_run_id = _normalize_run_id_token(current_run_id) or requested_run_id
                        if fallback_run_id:
                            current_run_id = fallback_run_id
                            if isinstance(target_ref, dict):
                                target_ref['run_id'] = fallback_run_id
                            if fallback_run_id not in run_ids:
                                run_ids.append(fallback_run_id)
                            run_ids = _dedupe_preserve_order(run_ids)
                            if not _run_marked_cancelled(problem_id, workspace_id, fallback_run_id):
                                _record_async_run_failure(
                                    problem,
                                    user,
                                    fallback_run_id,
                                    mode=verification_mode,
                                    source_label=source_path,
                                    error=str(target_exc),
                                    build_id=build_id,
                                    invocation_id=invocation_id,
                                    invocation_run_ids=run_ids,
                                    expected_behavior=expected_behavior,
                                    invocation_source='verification.start',
                                )
                            run_row = config.db.fetch_one('SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?', [fallback_run_id, problem_id, workspace_id])
                            summary_obj = _parse_summary_json(run_row['summary_json'] if run_row is not None else None, f'verification/{fallback_run_id}')
                        else:
                            summary_obj = {'error': str(target_exc)}
                    _store_target_result(spec, current_run_id=current_run_id, run_row=run_row, summary_obj=summary_obj)
                _pump_submit()

        for target in targets:
            token = _normalize_run_id_token(target.get('run_id'))
            if token and token not in run_ids:
                run_ids.append(token)
        run_ids = _dedupe_preserve_order(run_ids)

        # Retry accepted-solution mismatches once in serial to reduce transient
        # timing flakiness under heavy parallel judgehost load.
        retry_specs: list[dict[str, object]] = []
        for spec in target_specs:
            spec_index = int(spec.get('index') or 0)
            item = solution_results_by_index.get(spec_index)
            if not isinstance(item, dict):
                continue
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or spec.get('expected_behavior') or 'unknown'))
            if expected_behavior != 'accepted':
                continue
            if bool(item.get('matched')):
                continue
            run_status = str(item.get('run_status') or '').strip().lower()
            if run_status in {'running', 'queued', 'pending'}:
                continue
            retry_specs.append(spec)

        for spec in retry_specs:
            if _invocation_cancel_requested():
                break
            spec_index = int(spec.get('index') or 0)
            previous_item = solution_results_by_index.get(spec_index)
            previous_run_id = ''
            if isinstance(previous_item, dict):
                previous_run_id = _normalize_run_id_token(previous_item.get('run_id'))
            source_path = str(spec.get('source_path') or '').strip()
            target_ref = spec.get('target')
            retry_run_id = _allocate_run_id()
            prepared_payload: dict[str, object] | None = None
            if active_backend_name == 'domjudge-judgehost':
                prepared_payload = config.judgehost_task_service.prepare_enqueue_payload(
                    problem=problem,
                    username=user,
                    build_id=build_id,
                    mode=verification_mode,
                    submission_path=source_path,
                    upload_content=None,
                    upload_filename=None,
                    run_id=retry_run_id,
                    selected_tests=[],
                    invocation_id=invocation_id,
                    invocation_run_ids=list(run_ids),
                    expected_behavior='accepted',
                    invocation_source='verification.start',
                )
            try:
                submitted_run_id = str(
                    config.invocation_backend_service.run_submission(
                        problem=problem,
                        username=user,
                        build_id=build_id,
                        submission_path=source_path,
                        mode=verification_mode,
                        run_id=retry_run_id,
                        invocation_id=invocation_id,
                        invocation_run_ids=list(run_ids),
                        expected_behavior='accepted',
                        invocation_source='verification.start',
                        prepared_payload=prepared_payload,
                    )
                    or ''
                ).strip()
                normalized_submitted = _normalize_run_id_token(submitted_run_id)
                if normalized_submitted:
                    retry_run_id = normalized_submitted
                run_row = config.db.fetch_one(
                    'SELECT status,summary_json FROM runs WHERE id=? AND problem_id=? AND workspace_id=?',
                    [retry_run_id, problem_id, workspace_id],
                )
                if run_row is None:
                    continue
                summary_obj = _parse_summary_json(
                    run_row['summary_json'] if run_row is not None else None,
                    f'verification/{retry_run_id}',
                )
                _store_target_result(spec, current_run_id=retry_run_id, run_row=run_row, summary_obj=summary_obj)
                if isinstance(target_ref, dict):
                    target_ref['run_id'] = retry_run_id
                replaced = False
                rewritten_run_ids: list[str] = []
                for token in run_ids:
                    if (not replaced) and previous_run_id and token == previous_run_id:
                        rewritten_run_ids.append(retry_run_id)
                        replaced = True
                    else:
                        rewritten_run_ids.append(token)
                if (not replaced) and retry_run_id:
                    rewritten_run_ids.append(retry_run_id)
                run_ids = _dedupe_preserve_order(rewritten_run_ids)
            except Exception:
                # Keep original mismatch result when retry itself fails.
                continue

        solution_results: list[dict[str, object]] = []
        first_reason = ''
        for spec in target_specs:
            spec_index = int(spec.get('index') or 0)
            item = solution_results_by_index.get(spec_index)
            if not isinstance(item, dict):
                continue
            if not bool(item.get('matched')) and (not first_reason):
                first_reason = _verification_solution_failure_hint(
                    str(item.get('source_path') or ''),
                    str(item.get('reason') or ''),
                    str(item.get('error') or ''),
                )
            solution_results.append(item)
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
        safe_build_status = str(verification_details.get('build_status') or '').strip().lower()
        build_stage_failed = safe_build_status in {'failed', 'error', 'missing'} or str(exc).strip().lower().startswith('build failed')
        _backfill_missing_verification_runs(
            str(exc),
            build_for_failure=build_id,
            execution_skipped_for_missing=build_stage_failed,
        )
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
    run_id = run_ids[0] if run_ids else ''
    verification_details['run_id'] = run_id
    verification_details['run_ids'] = list(run_ids)
    verification_details['run_count'] = len(run_ids)
    _audit(actor_user_id, problem_id, 'verification.start', verification_details)

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
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'verification-{thread_name}',
            fn=_runner,
            queue_name='verification',
            backend=config.invocation_backend_service.active_backend_name(),
            dedupe_key=f'verification:{key}',
            job_type='verification',
        )
        worker_ref[0] = worker
        if not queued:
            with config.verification_lock:
                config.verification_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'verification queue rejected ({submit_reason})')
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

def _export_workspace_key(problem_id: int, workspace_id: int, head_commit: str, export_type: str) -> str:
    return f"{int(problem_id)}:{int(workspace_id)}:{str(head_commit or '').strip()}:{str(export_type or '').strip().lower()}"

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
        worker, queued, submit_reason = config.worker_queue_service.submit(
            name=f'export-{thread_name}',
            fn=_runner,
            queue_name='export',
            backend=config.sandbox_backend.name,
            dedupe_key=f'export:{key}',
            job_type='export',
        )
        worker_ref[0] = worker
        if not queued:
            with config.export_lock:
                config.export_inflight.discard(key)
            if submit_reason == 'dedupe_inflight':
                return False
            raise RuntimeError(f'export queue rejected ({submit_reason})')
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
    return {'mode': 'repository' if repo_exists else 'missing', 'display': _source_basename_label(repo_source) if repo_exists else 'missing', 'repo_source': repo_source, 'repo_source_exists': bool(repo_exists)}

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
    repo_source, repo_exists = _component_repo_source_from_build_cfg(workspace, build_cfg, 'checker_source', 'checkers', 'checker.cpp')
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
    has_destructive_sudo = _has_sudo_session(
        request,
        user_id=int(ctx['user']['id']),
        scope=str(_C.SUDO_SCOPE_DESTRUCTIVE),
    )
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
    return _template_response(request, 'workspace.html', {'ctx': ctx, 'status': status, 'branches': ctx.get('branches', []), 'message': message, 'selected_path': selected_path, 'selected_diff': selected_diff, 'selected_diff_truncated': bool(selected_diff_truncated), 'selected_diff_lines': selected_diff_lines, 'change_rows': change_rows, 'has_destructive_sudo': bool(has_destructive_sudo)})

def _tests_spec_resolve_index(raw_index: str, total: int) -> int:
    idx = _coerce_int(raw_index, 0, 1, max(1, total))
    if idx < 1 or idx > total:
        raise ValueError('invalid test index')
    return idx

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

def _run_rejudge_context_for_entries(entries: list[dict[str, object]], workspace: Path) -> dict[str, str | list[str]]:
    if not entries:
        return {'paths': [], 'query': '', 'unavailable_reason': 'no reusable solutions source'}
    statuses = [str(item.get('status') or '').strip().lower() for item in entries if isinstance(item, dict)]
    if any((status in {'running', 'queued', 'pending'} for status in statuses)):
        return {'paths': [], 'query': '', 'unavailable_reason': 'invocation still running'}
    reusable_paths: list[str] = []
    unavailable_reasons: list[str] = []
    all_reusable = True
    for item in entries:
        if not isinstance(item, dict):
            continue
        source = str(item.get('source') or '').strip()
        safe_solution, unavailable_reason = _run_rejudge_source_context(source, workspace)
        if safe_solution:
            reusable_paths.append(safe_solution)
        else:
            all_reusable = False
            if unavailable_reason:
                unavailable_reasons.append(unavailable_reason)
    deduped_paths = _dedupe_preserve_order(reusable_paths)
    if all_reusable and deduped_paths:
        return {
            'paths': deduped_paths,
            'query': '&'.join((f'solution_paths={quote_plus(path)}' for path in deduped_paths)),
            'unavailable_reason': '',
        }
    return {
        'paths': [],
        'query': '',
        'unavailable_reason': _summarize_rejudge_unavailable_reason(unavailable_reasons),
    }

def _run_invocation_status_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    statuses = [str(item.get('status') or '').strip().lower() for item in entries if isinstance(item, dict)]
    has_running = any((status in {'running', 'queued', 'pending'} for status in statuses))
    matched_count = sum((1 for item in entries if isinstance(item, dict) and bool(item.get('matched'))))
    total_count = len(entries)
    if has_running:
        status_text = 'running'
    else:
        status_text = 'ok' if total_count > 0 and matched_count == total_count else 'failed'
    return {
        'status': status_text,
        'status_upper': status_text.upper(),
        'is_failed': status_text == 'failed',
        'has_running': has_running,
        'matched_count': matched_count,
        'total_count': total_count,
    }

def _run_verification_details_from_audit(problem_id: int, actor_user_id: int, invocation_id: str, limit: int=240) -> dict[str, object]:
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if not safe_invocation_id:
        return {}
    rows = config.db.fetch_all(
        "\n        SELECT details_json,created_at\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action='verification.start'\n        ORDER BY created_at DESC\n        LIMIT ?\n        ",
        [int(problem_id), int(actor_user_id), max(40, int(limit))],
    )
    matched_verification: dict[str, object] = {}
    matched_verification_created = ''
    for row in rows:
        details: dict[str, object] = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        if _normalize_run_id_token(details.get('invocation_id')) != safe_invocation_id:
            continue
        matched_verification = details
        matched_verification_created = str(row['created_at'] or '').strip()
        break
    cancel_rows = config.db.fetch_all(
        """
        SELECT details_json,created_at
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='run.cancel'
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), max(40, int(limit))],
    )
    matched_cancel: dict[str, object] = {}
    matched_cancel_created = ''
    for row in cancel_rows:
        details: dict[str, object] = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        if _normalize_run_id_token(details.get('invocation_id')) != safe_invocation_id:
            continue
        matched_cancel = details
        matched_cancel_created = str(row['created_at'] or '').strip()
        break
    if matched_cancel:
        use_cancel = False
        if not matched_verification:
            use_cancel = True
        else:
            cancel_ts = _parse_iso_utc(matched_cancel_created)
            verification_ts = _parse_iso_utc(matched_verification_created)
            if verification_ts is None:
                use_cancel = True
            elif cancel_ts is not None:
                use_cancel = cancel_ts >= verification_ts
            else:
                use_cancel = True
        if use_cancel:
            merged_details: dict[str, object] = dict(matched_verification) if isinstance(matched_verification, dict) else {}
            merged_details['invocation_id'] = safe_invocation_id
            merged_details['status'] = 'failed'
            merged_details['cancelled'] = True
            cancel_reason = str(matched_cancel.get('reason') or '').strip() or 'verification cancelled by user'
            if cancel_reason:
                merged_details['error'] = cancel_reason
            if not isinstance(merged_details.get('run_ids'), list):
                cancel_run_ids = matched_cancel.get('run_ids')
                if isinstance(cancel_run_ids, list):
                    merged_details['run_ids'] = [str(item or '').strip() for item in cancel_run_ids if _normalize_run_id_token(item)]
            return {
                'details': merged_details,
                'created_at': matched_cancel_created or matched_verification_created,
            }
    if matched_verification:
        return {
            'details': matched_verification,
            'created_at': matched_verification_created,
        }
    return {}

def _run_lifecycle_status_label(status: str) -> str:
    token = str(status or '').strip().lower()
    if token == 'done':
        return 'Completed'
    if token == 'running':
        return 'In progress'
    if token in {'failed', 'interrupted'}:
        return 'Failed'
    if token == 'skipped':
        return 'Skipped'
    return 'Pending'

def _run_lifecycle_current_step(steps: list[dict[str, object]]) -> tuple[int, str]:
    if not steps:
        return (0, '-')
    for step in steps:
        status = str(step.get('status') or '').strip().lower()
        if status in {'running', 'failed', 'interrupted'}:
            try:
                return (int(step.get('index') or 0), str(step.get('title') or '-'))
            except Exception:
                return (0, str(step.get('title') or '-'))
    for step in steps:
        status = str(step.get('status') or '').strip().lower()
        if status == 'pending':
            try:
                return (int(step.get('index') or 0), str(step.get('title') or '-'))
            except Exception:
                return (0, str(step.get('title') or '-'))
    last = steps[-1]
    try:
        return (int(last.get('index') or 0), str(last.get('title') or '-'))
    except Exception:
        return (0, str(last.get('title') or '-'))

def _run_lifecycle_current_step_fields(steps: list[dict[str, object]], current_step_index: int) -> tuple[str, str, str]:
    safe_index = max(0, int(current_step_index))
    for step in steps:
        try:
            step_index = int(step.get('index') or 0)
        except Exception:
            step_index = 0
        if step_index != safe_index:
            continue
        status = str(step.get('status') or 'pending').strip().lower() or 'pending'
        status_label = str(step.get('status_label') or _run_lifecycle_status_label(status)).strip() or 'pending'
        detail = str(step.get('detail') or '').strip()
        return (status, status_label, detail)
    return ('pending', _run_lifecycle_status_label('pending'), '')

def _normalize_verification_step_id(raw: object) -> str:
    token = str(raw or '').strip().lower()
    if not token:
        return ''
    normalized = re.sub(r'[^a-z0-9._-]+', '', token)
    if normalized in {'gen', 'generate', 'generation'}:
        return 'gen'
    if normalized in {'val', 'validate', 'validation'}:
        return 'val'
    if normalized in {'run', 'execute'}:
        return 'run'
    if normalized in {'check', 'judge', 'verify'}:
        return 'check'
    return normalized

def _verification_step_title(step_id: str) -> str:
    token = str(step_id or '').strip().lower()
    if token == 'gen':
        return 'Generate Inputs'
    if token == 'val':
        return 'Generate Outputs'
    if token == 'run':
        return 'Run Solutions'
    if token == 'check':
        return 'Check Expectations'
    if not token:
        return 'Step'
    return token.replace('_', ' ').replace('-', ' ').strip().title()

def _verification_failed_build_step_id(step_hint: str, step_ids: list[str]) -> str:
    hint = str(step_hint or '').strip().lower()
    if not step_ids:
        return ''
    if ('check' in hint or 'judge' in hint or 'expect' in hint) and 'check' in step_ids:
        return 'check'
    # Build pipeline errors in validate/solve belong to output-generation stage.
    if (
        'val' in hint
        or 'validator' in hint
        or 'solve' in hint
        or 'answer' in hint
        or 'sample_output_validate' in hint
        or 'interactor' in hint
        or 'accepted' in hint
    ) and 'val' in step_ids:
        return 'val'
    if ('run' in hint or 'execute' in hint or 'submission' in hint) and 'run' in step_ids:
        return 'run'
    if ('gen' in hint or 'test' in hint or 'compile' in hint) and 'gen' in step_ids:
        return 'gen'
    if 'gen' in step_ids:
        return 'gen'
    return step_ids[0]

def _verification_tests_meta_stats(problem_slug: str, build_id: str) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'total': 0,
        'manual': 0,
        'gen': 0,
        'sample': 0,
    }
    safe_problem = str(problem_slug or '').strip()
    safe_build_id = str(build_id or '').strip()
    if (not safe_problem) or (not is_canonical_artifact_id(safe_build_id)):
        return stats
    try:
        root = _artifact_root(safe_problem, safe_build_id)
    except HTTPException:
        return stats
    tests_meta_path = root / 'logs' / 'tests_meta.json'
    try:
        if tests_meta_path.exists() and tests_meta_path.is_file() and (not tests_meta_path.is_symlink()):
            payload = json.loads(tests_meta_path.read_text(encoding='utf-8', errors='replace'))
            if isinstance(payload, list):
                total = 0
                manual = 0
                generated = 0
                sample = 0
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    total += 1
                    kind = str(item.get('kind') or '').strip().lower()
                    if kind == 'manual':
                        manual += 1
                    elif kind == 'gen':
                        generated += 1
                    if bool(item.get('sample')):
                        sample += 1
                stats.update({
                    'loaded': True,
                    'total': total,
                    'manual': manual,
                    'gen': generated,
                    'sample': sample,
                })
                return stats
    except Exception:
        pass
    tests_dir = root / 'tests'
    names: list[str] = []
    try:
        if tests_dir.exists() and tests_dir.is_dir() and (not tests_dir.is_symlink()):
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
    except Exception:
        names = []
    if names:
        stats.update({
            'loaded': True,
            'total': len(names),
            'manual': 0,
            'gen': 0,
            'sample': 0,
        })
    return stats

def _verification_validate_stats(problem_slug: str, build_id: str) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'truncated': False,
        'total': 0,
        'ok': 0,
        'failed': 0,
        'timed_out': 0,
    }
    safe_problem = str(problem_slug or '').strip()
    safe_build_id = str(build_id or '').strip()
    if (not safe_problem) or (not is_canonical_artifact_id(safe_build_id)):
        return stats
    try:
        root = _artifact_root(safe_problem, safe_build_id)
    except HTTPException:
        return stats
    validate_log = root / 'logs' / 'validate.log'
    try:
        if (not validate_log.exists()) or (not validate_log.is_file()) or validate_log.is_symlink():
            return stats
    except OSError:
        return stats
    total = 0
    ok_count = 0
    failed_count = 0
    timed_out_count = 0
    seen: set[str] = set()
    max_lines = 200000
    line_count = 0
    try:
        with validate_log.open('r', encoding='utf-8', errors='replace') as fh:
            for raw_line in fh:
                line_count += 1
                if line_count > max_lines:
                    stats['truncated'] = True
                    break
                line = str(raw_line or '').strip()
                if not line:
                    continue
                if ': ' not in line:
                    continue
                test_name, remainder = line.split(': ', 1)
                test_name = str(test_name or '').strip()
                if (not _C.RUN_TEST_NAME_RE.fullmatch(test_name)) or (test_name in seen):
                    continue
                if ' rc=' not in remainder:
                    continue
                seen.add(test_name)
                total += 1
                timed_out = 'timed_out=1' in remainder
                if timed_out:
                    timed_out_count += 1
                rc_token = remainder.rsplit(' rc=', 1)[-1].split()[0]
                try:
                    rc = int(rc_token)
                except Exception:
                    rc = -1
                if (not timed_out) and (rc in {0, 42}):
                    ok_count += 1
                else:
                    failed_count += 1
    except Exception:
        return stats
    if total <= 0:
        return stats
    stats.update({
        'loaded': True,
        'total': total,
        'ok': ok_count,
        'failed': failed_count,
        'timed_out': timed_out_count,
    })
    return stats


def _verification_output_stats(problem_slug: str, build_id: str) -> dict[str, object]:
    stats: dict[str, object] = {
        'loaded': False,
        'total': 0,
        'generated': 0,
    }
    safe_problem = str(problem_slug or '').strip()
    safe_build_id = str(build_id or '').strip()
    if (not safe_problem) or (not is_canonical_artifact_id(safe_build_id)):
        return stats
    try:
        root = _artifact_root(safe_problem, safe_build_id)
    except HTTPException:
        return stats
    try:
        if (not root.exists()) or (not root.is_dir()) or root.is_symlink():
            return stats
    except OSError:
        return stats
    stats['loaded'] = True
    tests_dir = root / 'tests'
    ans_dir = root / 'ans'
    test_names: set[str] = set()
    try:
        if tests_dir.exists() and tests_dir.is_dir() and (not tests_dir.is_symlink()):
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
                    test_names.add(name)
    except Exception:
        test_names = set()
    answered_tests: set[str] = set()
    try:
        if ans_dir.exists() and ans_dir.is_dir() and (not ans_dir.is_symlink()):
            with os.scandir(ans_dir) as entries:
                for entry in entries:
                    name = str(entry.name or '')
                    if not name.lower().endswith('.ans'):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    stem = Path(name).stem
                    test_name = f'{stem}.in'
                    if _C.RUN_TEST_NAME_RE.fullmatch(test_name):
                        answered_tests.add(test_name)
    except Exception:
        answered_tests = set()
    total = len(test_names)
    if total <= 0 and answered_tests:
        total = len(answered_tests)
    generated = len(answered_tests if not test_names else (answered_tests & test_names))
    stats.update(
        {
            'total': max(0, int(total)),
            'generated': max(0, int(generated)),
        }
    )
    return stats

def _verification_buildsolve_case_progress(build_id: str) -> dict[str, int]:
    safe_build_id = str(build_id or '').strip()
    if not is_canonical_artifact_id(safe_build_id):
        return {'total': 0, 'reported': 0}
    try:
        return dict(config.judgehost_task_service.domjudge_buildsolve_progress(safe_build_id))
    except Exception:
        return {'total': 0, 'reported': 0}

def _verification_selected_tests_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    try:
        selected_count = int(summary.get('selected_tests_count') or 0)
    except Exception:
        selected_count = 0
    if selected_count > 0:
        return selected_count
    selected_tests_raw = summary.get('selected_tests')
    if isinstance(selected_tests_raw, list):
        selected_tests = _dedupe_preserve_order(
            [_normalize_run_test_name_token(item) for item in selected_tests_raw]
        )
        if selected_tests:
            return len(selected_tests)
    return _run_test_count_from_summary(summary)

def _verification_judgehost_case_progress(run_ids: list[str]) -> dict[str, dict[str, int]]:
    safe_run_ids = _dedupe_preserve_order([_normalize_run_id_token(item) for item in run_ids if _normalize_run_id_token(item)])
    if not safe_run_ids:
        return {}
    try:
        return dict(config.judgehost_task_service.domjudge_case_progress_for_runs(safe_run_ids))
    except Exception:
        return {}


def _run_domjudge_verdict_from_runresult(raw: object) -> str:
    token = str(raw or '').strip().lower()
    mapping = {
        'correct': 'OK',
        'compiler-error': 'CE',
        'timelimit': 'TL',
        'run-error': 'RE',
        'wrong-answer': 'WA',
        'no-output': 'WA',
        'output-limit': 'FL',
        'compare-error': 'FL',
        'internal-error': 'FL',
    }
    return str(mapping.get(token, 'FL'))


def _run_domjudge_case_cells(run_ids: list[str]) -> dict[str, dict[str, dict[str, object]]]:
    safe_run_ids = _dedupe_preserve_order([_normalize_run_id_token(item) for item in run_ids if _normalize_run_id_token(item)])
    if not safe_run_ids:
        return {}
    try:
        rows = list(config.judgehost_task_service.domjudge_case_cells_for_runs(safe_run_ids))
    except Exception:
        return {}
    out: dict[str, dict[str, dict[str, object]]] = {}
    for row in rows:
        run_id = _normalize_run_id_token(row['run_id'])
        if not run_id:
            continue
        test_name = _normalize_run_test_name_token(row['test_name'])
        if not test_name:
            continue
        status = str(row['status'] or '').strip().lower()
        runresult = str(row['runresult'] or '').strip().lower()
        cpu_sec = 0.0
        runtime_sec = 0.0
        wall_sec = 0.0
        memory_kb = 0
        try:
            cpu_sec = max(0.0, float(row['cpu_sec'] or 0.0))
        except Exception:
            cpu_sec = 0.0
        try:
            runtime_sec = max(0.0, float(row['runtime_sec'] or 0.0))
        except Exception:
            runtime_sec = 0.0
        try:
            wall_sec = max(0.0, float(row['wall_sec'] or 0.0))
        except Exception:
            wall_sec = 0.0
        try:
            memory_kb = max(0, int(row['memory_kb'] or 0))
        except Exception:
            memory_kb = 0

        verdict = ''
        short = '..'
        metrics = 'pending'
        cpu_ms = int(round(max(cpu_sec, runtime_sec) * 1000.0))
        wall_ms = int(round(max(wall_sec, cpu_sec, runtime_sec) * 1000.0))
        reported = status == 'reported'
        if reported:
            verdict = _run_domjudge_verdict_from_runresult(runresult)
            short = _run_verdict_short(verdict)
            metrics = f'{cpu_ms}ms/{_run_memory_mb_text(memory_kb)}'
        elif status == 'leased':
            metrics = 'running'

        by_run = out.setdefault(run_id, {})
        by_run[test_name] = {
            'test_name': test_name,
            'status': status,
            'reported': bool(reported),
            'verdict': verdict,
            'short': short,
            'time_ms': int(cpu_ms),
            'cpu_ms': int(cpu_ms),
            'wall_ms': int(wall_ms),
            'memory_kb': int(memory_kb),
            'metrics': metrics,
        }
    return out

def _verification_run_test_progress(
    *,
    materialized_columns: list[dict[str, object]],
    run_statuses: list[str],
    run_count: int,
    fallback_tests_per_solution: int,
) -> dict[str, int]:
    safe_run_count = max(0, int(run_count))
    safe_fallback_tests = max(0, int(fallback_tests_per_solution))
    run_ids = [_normalize_run_id_token(col.get('id')) for col in materialized_columns if isinstance(col, dict)]
    case_progress_by_run = _verification_judgehost_case_progress([run_id for run_id in run_ids if run_id])
    total_tests = 0
    completed_tests = 0
    running_tests = 0
    started_runs = 0
    for idx, col in enumerate(materialized_columns):
        if not isinstance(col, dict):
            continue
        started_runs += 1
        run_id = _normalize_run_id_token(col.get('id'))
        summary = col.get('summary') if isinstance(col.get('summary'), dict) else None
        expected_tests = _verification_selected_tests_count(summary)
        if expected_tests <= 0:
            expected_tests = safe_fallback_tests
        case_progress = case_progress_by_run.get(run_id, {})
        case_total = max(0, int(case_progress.get('total') or 0))
        case_reported = max(0, int(case_progress.get('reported') or 0))
        case_leased = max(0, int(case_progress.get('leased') or 0))
        if case_total > 0:
            expected_tests = max(expected_tests, case_total)
        reported_tests = _run_test_count_from_summary(summary)
        reported_tests = max(reported_tests, case_reported)
        if expected_tests > 0:
            reported_tests = min(expected_tests, max(0, int(reported_tests)))
        else:
            reported_tests = max(0, int(reported_tests))
            expected_tests = reported_tests
        total_tests += expected_tests
        completed_tests += reported_tests
        run_status = str(run_statuses[idx] if idx < len(run_statuses) else '').strip().lower()
        if case_leased > 0:
            running_tests += case_leased
        elif run_status in {'running', 'queued', 'pending'} and expected_tests > reported_tests:
            running_tests += (expected_tests - reported_tests)
    remaining_runs = max(0, safe_run_count - started_runs)
    if remaining_runs > 0 and safe_fallback_tests > 0:
        total_tests += remaining_runs * safe_fallback_tests
    return {
        'total': max(0, int(total_tests)),
        'completed': max(0, int(completed_tests)),
        'running': max(0, int(running_tests)),
    }

def _build_verification_lifecycle_card(
    *,
    problem_slug: str,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    invocation_id: str,
    verification_details: dict[str, object],
    columns: list[dict[str, object]],
    detail_status: str,
    detail_running: bool,
    progress_reported: int,
    progress_total: int,
    matched_count: int,
    match_total: int,
) -> dict[str, object]:
    raw_steps = verification_details.get('steps')
    step_ids: list[str] = []
    if isinstance(raw_steps, list):
        for item in raw_steps:
            token = _normalize_verification_step_id(item)
            if token and token not in step_ids:
                step_ids.append(token)
    if not step_ids:
        step_ids = ['gen', 'val', 'run', 'check']
    if 'run' not in step_ids:
        step_ids.append('run')
    if 'check' not in step_ids:
        step_ids.append('check')
    status_by_step = {token: 'pending' for token in step_ids}
    detail_by_step: dict[str, str] = {}

    build_id = str(verification_details.get('build_id') or '').strip()
    if (not is_canonical_artifact_id(build_id)):
        build_id = ''
        for col in columns:
            candidate = str(col.get('build_id') or '').strip()
            if is_canonical_artifact_id(candidate):
                build_id = candidate
                break
    build_status = str(verification_details.get('build_status') or '').strip().lower()
    has_materialized_columns = any(
        bool(col.get('has_run_row')) for col in columns if isinstance(col, dict)
    )
    if (not build_id) and bool(detail_running) and (not has_materialized_columns):
        inflight_build = config.db.fetch_one(
            """
            SELECT id,status
            FROM builds
            WHERE problem_id=? AND workspace_id=? AND status IN ('running','queued','pending')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [int(problem_id), int(workspace_id)],
        )
        if inflight_build is not None:
            candidate = str(inflight_build['id'] or '').strip()
            if is_canonical_artifact_id(candidate):
                build_id = candidate
                if not str(build_status or '').strip():
                    build_status = str(inflight_build['status'] or '').strip().lower()
    build_failed_step = str(verification_details.get('build_failed_step') or '').strip()
    build_failed_test = str(verification_details.get('build_failed_test') or '').strip()
    build_error_text = _compact_error_text(str(verification_details.get('build_error') or ''))
    if build_id:
        build_row = config.db.fetch_one(
            'SELECT status,summary_json FROM builds WHERE id=? AND problem_id=? AND workspace_id=?',
            [build_id, int(problem_id), int(workspace_id)],
        )
        if build_row is not None:
            status_token = str(build_row['status'] or '').strip().lower()
            if status_token:
                build_status = status_token
            if not build_failed_step:
                build_summary = _parse_summary_json(build_row['summary_json'], f'verification/build/{build_id}')
                build_failed_step = str(build_summary.get('failed_step') or '').strip() if isinstance(build_summary, dict) else ''
                build_failed_test = str(build_summary.get('failed_test') or '').strip() if isinstance(build_summary, dict) else build_failed_test
                if (not build_error_text):
                    build_error_text = _compact_error_text(str(build_summary.get('error') or '')) if isinstance(build_summary, dict) else ''
    if build_id:
        detail_by_step['gen'] = 'build prepared'

    running_statuses = {'running', 'queued', 'pending'}
    materialized_columns = [
        col
        for col in columns
        if isinstance(col, dict) and bool(col.get('has_run_row'))
    ]
    run_statuses = [str(col.get('status') or '').strip().lower() for col in materialized_columns]
    has_started_runs = bool(materialized_columns)
    has_running_runs = any((item in running_statuses for item in run_statuses))
    invocation_backend_name = str(verification_details.get('invocation_backend') or '').strip().lower()
    if not invocation_backend_name:
        for col in materialized_columns:
            summary_obj = col.get('summary')
            if not isinstance(summary_obj, dict):
                continue
            token = str(summary_obj.get('invocation_backend') or '').strip().lower()
            if token:
                invocation_backend_name = token
                break
    prefer_case_progress = invocation_backend_name == 'domjudge-judgehost'
    invocation_sources: set[str] = set()
    for col in materialized_columns:
        summary_obj = col.get('summary')
        if not isinstance(summary_obj, dict):
            continue
        inv_block = summary_obj.get('invocation')
        if not isinstance(inv_block, dict):
            continue
        source_token = str(inv_block.get('source') or '').strip().lower()
        if source_token:
            invocation_sources.add(source_token)
    if not invocation_sources and isinstance(verification_details, dict):
        details_source = str(verification_details.get('source') or '').strip().lower()
        if details_source:
            invocation_sources.add(details_source)
    buildsolve_only = bool(invocation_sources) and invocation_sources.issubset({'build.solve'})
    if buildsolve_only:
        has_started_runs = False
        has_running_runs = False
    if has_started_runs and bool(detail_running):
        has_running_runs = True
    completed_runs = sum((1 for item in run_statuses if item and item not in running_statuses))
    failed_run_count = 0
    cancelled_run_count = 0
    for idx, col in enumerate(materialized_columns):
        run_status = run_statuses[idx] if idx < len(run_statuses) else str(col.get('status') or '').strip().lower()
        summary_obj = col.get('summary')
        cancelled_this_run = False
        if isinstance(summary_obj, dict):
            if bool(summary_obj.get('cancelled')):
                cancelled_this_run = True
            else:
                summary_error_text = str(summary_obj.get('error') or '').strip().lower()
                if ('cancelled by user' in summary_error_text) or ('verification cancelled' in summary_error_text):
                    cancelled_this_run = True
        if cancelled_this_run:
            cancelled_run_count += 1
            continue
        if run_status == 'failed':
            failed_run_count += 1
    run_skip_flags = [bool(col.get('execution_skipped')) for col in columns if isinstance(col, dict)]
    all_runs_skipped = bool(run_skip_flags) and all(run_skip_flags)
    safe_detail_status = str(detail_status or '').strip().lower()
    error_text = _compact_error_text(str(verification_details.get('error') or ''))
    error_text_lower = error_text.lower()
    cancelled_from_error = ('cancelled by user' in error_text_lower) or ('verification cancelled' in error_text_lower)
    cancelled_from_details = bool(verification_details.get('cancelled'))
    cancelled_from_audit = False
    safe_invocation_id = _normalize_run_id_token(invocation_id)
    if safe_invocation_id and int(actor_user_id) > 0:
        try:
            cancelled_from_audit = _invocation_marked_cancelled(int(problem_id), int(actor_user_id), safe_invocation_id)
        except Exception:
            cancelled_from_audit = False
    run_interrupted = bool(cancelled_run_count > 0 or cancelled_from_error or cancelled_from_details or cancelled_from_audit)
    try:
        run_count = max(0, int(verification_details.get('run_count') or 0))
    except Exception:
        run_count = 0
    if run_count <= 0:
        run_count = len(columns)
    run_count = max(run_count, len(columns))
    has_any_runs = bool(run_count > 0 and (has_started_runs or build_status in {'ok', 'failed', 'error'}))
    if has_any_runs:
        completed_runs = min(run_count, max(0, int(completed_runs)))
    test_progress = _verification_run_test_progress(
        materialized_columns=materialized_columns,
        run_statuses=run_statuses,
        run_count=run_count,
        fallback_tests_per_solution=max(0, int(progress_total)),
    )
    total_test_units = int(test_progress.get('total') or 0)
    completed_test_units = int(test_progress.get('completed') or 0)
    running_test_units = int(test_progress.get('running') or 0)
    run_failed = False
    if (not all_runs_skipped) and has_started_runs and (not has_running_runs) and (not run_interrupted):
        if failed_run_count > 0:
            run_failed = True
        elif safe_detail_status == 'failed':
            run_failed = True
    progress_label = ''
    if all_runs_skipped:
        if run_interrupted:
            progress_label = 'not executed (cancelled)'
        else:
            progress_label = 'not executed (build failed)'
    elif total_test_units > 0 and has_started_runs and has_running_runs:
        progress_label = f'{completed_test_units}/{total_test_units} tests finished'
    elif run_interrupted:
        if total_test_units > 0:
            progress_label = f'failed ({completed_test_units}/{total_test_units} completed)'
        elif run_count > 0:
            progress_label = f'failed ({completed_runs}/{run_count} completed)'
        else:
            progress_label = 'failed'
    elif run_failed:
        if total_test_units > 0:
            progress_label = f'failed ({completed_test_units}/{total_test_units} completed)'
        elif run_count > 0:
            progress_label = f'failed ({completed_runs}/{run_count} completed)'
        else:
            progress_label = 'failed'
    elif total_test_units > 0 and has_started_runs:
        progress_label = f'{completed_test_units}/{total_test_units} tests finished'
    elif build_status == 'ok' and total_test_units > 0:
        progress_label = f'0/{total_test_units} tests finished'
    elif has_started_runs:
        progress_label = f'{completed_runs}/{run_count} solutions finished'
    elif build_status == 'ok' and run_count > 0:
        progress_label = f'0/{run_count} solutions finished'
    if progress_label:
        detail_by_step['run'] = progress_label
    if match_total > 0:
        detail_by_step['check'] = f'matched expectations {int(matched_count)}/{int(match_total)}'
    if error_text and (build_status not in {'failed', 'error'}) and (not run_interrupted):
        detail_by_step['check'] = error_text

    tests_meta_stats = _verification_tests_meta_stats(problem_slug, build_id)
    tests_meta_loaded = bool(tests_meta_stats.get('loaded'))
    generated_total = max(0, int(tests_meta_stats.get('total') or 0))
    generated_manual = max(0, int(tests_meta_stats.get('manual') or 0))
    generated_from_gen = max(0, int(tests_meta_stats.get('gen') or 0))
    if generated_total <= 0 and progress_total > 0:
        generated_total = max(0, int(progress_total))

    output_stats = _verification_output_stats(problem_slug, build_id)
    outputs_total = max(0, int(output_stats.get('total') or 0))
    outputs_generated = max(0, int(output_stats.get('generated') or 0))
    buildsolve_case_total = 0
    buildsolve_case_reported = 0
    if build_id:
        buildsolve_progress = _verification_buildsolve_case_progress(build_id)
        case_total = max(0, int(buildsolve_progress.get('total') or 0))
        case_reported = max(0, int(buildsolve_progress.get('reported') or 0))
        buildsolve_case_total = case_total
        buildsolve_case_reported = case_reported
        if case_total > 0:
            if build_status in {'running', 'queued', 'pending'}:
                # While build is running, case-level progress is authoritative for
                # output-generation progress. File-based ans counts can include stale
                # artifacts from previous attempts.
                outputs_total = case_total
                outputs_generated = min(case_reported, case_total)
            else:
                outputs_total = max(outputs_total, case_total)
                outputs_generated = max(outputs_generated, min(case_reported, case_total))
    if outputs_total <= 0 and generated_total > 0:
        outputs_total = generated_total
    if outputs_generated > outputs_total:
        outputs_total = outputs_generated
    if (
        build_status in {'running', 'queued', 'pending'}
        and prefer_case_progress
        and (not has_started_runs)
        and build_id
        and buildsolve_case_total <= 0
    ):
        # Judgehost case progress has not been registered yet; avoid showing stale
        # ans-file totals from previous runs (for example transient 27/27 while still running).
        outputs_total = 0
        outputs_generated = 0
    validate_stats = _verification_validate_stats(problem_slug, build_id)
    validated_total = max(0, int(validate_stats.get('total') or 0))
    validated_ok = max(0, int(validate_stats.get('ok') or 0))
    validated_failed = max(0, int(validate_stats.get('failed') or 0))
    validated_timed_out = max(0, int(validate_stats.get('timed_out') or 0))
    if validated_total <= 0 and build_status == 'ok' and generated_total > 0:
        validated_total = generated_total
        validated_ok = generated_total
        validated_failed = 0
        validated_timed_out = 0

    build_failed = build_status in {'failed', 'error'}
    build_running = build_status in {'running', 'queued', 'pending'}
    build_done = build_status == 'ok'
    if (not build_status) and has_started_runs:
        build_done = True
    if (not build_status) and (not has_started_runs):
        # Initial verification audit entry is written before build starts; keep
        # lifecycle focused on "Generate Inputs" instead of jumping to run stage.
        build_running = True
    outputs_phase_done_while_build_running = bool(has_started_runs)
    if (not outputs_phase_done_while_build_running) and buildsolve_case_total > 0:
        outputs_phase_done_while_build_running = buildsolve_case_reported >= buildsolve_case_total

    failed_step_id = ''
    cancel_before_runs = run_interrupted and (all_runs_skipped or (not has_started_runs))
    if cancel_before_runs:
        cancel_detail = error_text or 'verification cancelled by user'
        # If generation has already produced tests (or build is already past initial
        # bootstrap), keep cancellation pinned to step-2 instead of regressing to step-1.
        if ('val' in step_ids) and (
            generated_total > 0
            or outputs_total > 0
            or validated_total > 0
            or build_status in {'ok', 'running', 'queued', 'pending'}
            or bool(build_id)
        ):
            failed_step_id = 'val'
        elif 'gen' in step_ids:
            failed_step_id = 'gen'
        elif 'val' in step_ids:
            failed_step_id = 'val'
        else:
            failed_step_id = step_ids[0]
        fail_index = step_ids.index(failed_step_id) if failed_step_id in step_ids else 0
        for idx, token in enumerate(step_ids):
            if idx < fail_index:
                status_by_step[token] = 'done'
            elif idx == fail_index:
                status_by_step[token] = 'failed'
            else:
                status_by_step[token] = 'skipped'
        if failed_step_id:
            detail_by_step[failed_step_id] = cancel_detail
        if 'run' in status_by_step:
            detail_by_step['run'] = 'not executed (cancelled)'
        if 'check' in status_by_step:
            detail_by_step['check'] = 'failed'
    elif build_failed:
        failed_step_id = _verification_failed_build_step_id(build_failed_step, step_ids)
        fail_index = step_ids.index(failed_step_id) if failed_step_id in step_ids else 0
        if error_text:
            safe_failed_hint = str(build_failed_step or '').strip().lower()
            if failed_step_id == 'val' and 'solve' in safe_failed_hint:
                if build_failed_test:
                    detail_by_step[failed_step_id] = f'output generation failed on {build_failed_test}'
                else:
                    detail_by_step[failed_step_id] = 'output generation failed'
            else:
                detail_by_step[failed_step_id] = error_text
        for idx, token in enumerate(step_ids):
            if idx < fail_index:
                status_by_step[token] = 'done'
            elif idx == fail_index:
                status_by_step[token] = 'failed'
            else:
                status_by_step[token] = 'skipped'
                if token == 'run':
                    detail_by_step[token] = 'not executed (build failed)'
                if token == 'check':
                    detail_by_step[token] = 'skipped'
    elif build_running:
        if generated_total > 0 or outputs_total > 0 or has_started_runs:
            if 'gen' in status_by_step:
                status_by_step['gen'] = 'done'
        if outputs_phase_done_while_build_running:
            if 'val' in status_by_step:
                status_by_step['val'] = 'done'
            if 'run' in status_by_step:
                if all_runs_skipped:
                    status_by_step['run'] = 'skipped'
                elif has_running_runs:
                    status_by_step['run'] = 'running'
                elif run_interrupted or run_failed:
                    status_by_step['run'] = 'failed'
                elif has_started_runs:
                    status_by_step['run'] = 'done'
                else:
                    status_by_step['run'] = 'pending'
            if 'check' in status_by_step:
                if run_interrupted:
                    status_by_step['check'] = 'skipped'
                    detail_by_step['check'] = 'failed'
                elif has_running_runs:
                    status_by_step['check'] = 'pending'
                elif safe_detail_status == 'ok' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'done'
                elif safe_detail_status == 'failed' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'failed'
                elif has_started_runs:
                    status_by_step['check'] = 'done'
                else:
                    status_by_step['check'] = 'pending'
                if status_by_step['check'] != 'pending':
                    check_idx = step_ids.index('check')
                    for token in step_ids[:check_idx]:
                        if status_by_step[token] == 'pending':
                            status_by_step[token] = 'done'
        elif generated_total > 0 or outputs_total > 0:
            if 'val' in status_by_step:
                status_by_step['val'] = 'running'
            else:
                for token in step_ids:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'running'
                        break
        else:
            running_step = 'gen' if 'gen' in status_by_step else step_ids[0]
            status_by_step[running_step] = 'running'
    else:
        if build_done:
            if 'gen' in status_by_step:
                status_by_step['gen'] = 'done'
            if 'val' in status_by_step:
                status_by_step['val'] = 'done'
            run_idx = step_ids.index('run') if 'run' in step_ids else -1
            if run_idx > 0:
                for token in step_ids[:run_idx]:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'done'
        if build_done and has_running_runs:
            if 'run' in status_by_step:
                status_by_step['run'] = 'running'
            else:
                for token in step_ids:
                    if status_by_step[token] == 'pending':
                        status_by_step[token] = 'running'
                        break
        else:
            if build_done and has_any_runs and 'run' in status_by_step:
                if all_runs_skipped:
                    status_by_step['run'] = 'skipped'
                elif run_interrupted:
                    status_by_step['run'] = 'failed'
                elif run_failed:
                    status_by_step['run'] = 'failed'
                elif has_started_runs:
                    status_by_step['run'] = 'done'
                else:
                    status_by_step['run'] = 'pending'
            if 'check' in status_by_step:
                if run_interrupted:
                    status_by_step['check'] = 'skipped'
                    detail_by_step['check'] = 'failed'
                elif safe_detail_status == 'ok' and (has_started_runs or all_runs_skipped):
                    status_by_step['check'] = 'done'
                elif safe_detail_status == 'failed' and (has_started_runs or all_runs_skipped or build_failed):
                    status_by_step['check'] = 'failed'
                elif has_started_runs:
                    status_by_step['check'] = 'done'
                else:
                    status_by_step['check'] = 'pending'
                if status_by_step['check'] != 'pending':
                    check_idx = step_ids.index('check')
                    for token in step_ids[:check_idx]:
                        if status_by_step[token] == 'pending':
                            status_by_step[token] = 'done'

    step_facts: dict[str, list[dict[str, str]]] = {token: [] for token in step_ids}
    step_notes: dict[str, list[str]] = {token: [] for token in step_ids}

    def _step_add_fact(step_id: str, label: str, value: object, tone: str='') -> None:
        token = str(step_id or '').strip()
        if token not in step_facts:
            return
        label_text = str(label or '').strip()
        value_text = str(value or '').strip()
        if (not label_text) or (not value_text):
            return
        row = {'label': label_text, 'value': value_text, 'tone': str(tone or '').strip().lower()}
        step_facts[token].append(row)

    def _step_add_note(step_id: str, text: object) -> None:
        token = str(step_id or '').strip()
        if token not in step_notes:
            return
        note_text = str(text or '').strip()
        if not note_text:
            return
        if note_text not in step_notes[token]:
            step_notes[token].append(note_text)

    gen_status = str(status_by_step.get('gen') or '').strip().lower()
    if gen_status != 'failed':
        if generated_total > 0:
            generated_count_label = _count_label(generated_total, 'test')
            detail_by_step['gen'] = f'generated {generated_count_label}'
        elif build_running and tests_meta_loaded:
            detail_by_step['gen'] = 'generating inputs'

    val_status_token = str(status_by_step.get('val') or '').strip().lower()
    if val_status_token != 'failed':
        if outputs_total > 0:
            if (val_status_token in {'pending', 'running'}) and build_running and (outputs_generated < outputs_total):
                detail_by_step['val'] = 'generating outputs'
            else:
                detail_by_step['val'] = f'generated outputs {outputs_generated}/{outputs_total}'
        elif build_running and generated_total > 0:
            detail_by_step['val'] = 'generating outputs'

    running_count = sum((1 for token in run_statuses if token in running_statuses))
    finished_count = max(0, int(completed_runs))
    if all_runs_skipped:
        if run_count > 0:
            _step_add_fact('run', 'Solutions skipped', f'{int(run_count)}/{int(run_count)}')
    else:
        if run_count > 0:
            if run_interrupted or run_failed:
                _step_add_fact('run', 'Solutions completed', f'{finished_count}/{int(run_count)}')
            else:
                _step_add_fact('run', 'Solutions finished', f'{finished_count}/{int(run_count)}')
        if total_test_units > 0:
            if run_interrupted or run_failed:
                _step_add_fact('run', 'Tests completed', f'{completed_test_units}/{total_test_units}')
            else:
                _step_add_fact('run', 'Tests finished', f'{completed_test_units}/{total_test_units}')
        if running_count > 0:
            _step_add_fact('run', 'Running solutions', _count_label(running_count, 'solution'))
        if running_test_units > 0:
            _step_add_fact('run', 'Running tests', _count_label(running_test_units, 'test'))
        if failed_run_count > 0:
            _step_add_fact('run', 'Failed solutions', _count_label(failed_run_count, 'solution'))
        if cancelled_run_count > 0:
            _step_add_fact('run', 'Cancelled solutions', _count_label(cancelled_run_count, 'solution'))
        if progress_total > 0:
            _step_add_fact('run', 'Tests per solution', _count_label(int(progress_total), 'test'))
    if all_runs_skipped:
        run_skip_note = str(detail_by_step.get('run') or '').strip() or 'not executed (build failed)'
        _step_add_note('run', run_skip_note)
    elif run_interrupted:
        _step_add_note('run', error_text or 'verification cancelled by user')
    elif run_failed and error_text:
        _step_add_note('run', error_text)
    elif build_status == 'ok' and run_count > 0 and (not has_started_runs):
        _step_add_note('run', 'Waiting for solution execution results.')

    if generated_total > 0 or (build_running and tests_meta_loaded):
        _step_add_fact('gen', 'Generated tests', _count_label(generated_total, 'test'))
        if generated_manual > 0:
            _step_add_fact('gen', 'Manual tests', _count_label(generated_manual, 'test'))
        if generated_from_gen > 0:
            _step_add_fact('gen', 'Generator tests', _count_label(generated_from_gen, 'test'))
    elif build_running:
        _step_add_note('gen', 'Build is preparing input generation.')
    if build_failed and failed_step_id == 'gen':
        if build_failed_test:
            _step_add_note('gen', f'Failed test: {build_failed_test}')
        if build_error_text:
            _step_add_note('gen', build_error_text)
        elif error_text:
            _step_add_note('gen', error_text)

    if outputs_total > 0:
        _step_add_fact('val', 'Generated outputs', f'{min(outputs_generated, outputs_total)}/{outputs_total}')

    if validated_total > 0:
        _step_add_fact('gen', 'Validated inputs', f'{validated_ok}/{validated_total}')
        if validated_failed > 0:
            _step_add_fact('gen', 'Failed validations', _count_label(validated_failed, 'test'), tone='danger')
        if validated_timed_out > 0:
            _step_add_fact('gen', 'Validation timeouts', _count_label(validated_timed_out, 'test'), tone='danger')
        if bool(validate_stats.get('truncated')):
            _step_add_note('gen', 'Validation log was truncated while summarizing.')
    elif build_status == 'ok' and generated_total > 0:
        _step_add_fact('gen', 'Validated inputs', f'{generated_total}/{generated_total}')
    elif (val_status_token in {'pending', 'running'}) and (build_status in {'running', 'queued', 'pending', ''}):
        if generated_total > 0:
            _step_add_note('val', 'Waiting for output generation results.')
        else:
            _step_add_note('val', 'Waiting for input generation results.')
    if (val_status_token in {'pending', 'running'}) and build_running and outputs_total > 0 and outputs_generated < outputs_total:
        _step_add_note('val', 'Running accepted solution to generate outputs.')
    if build_failed and failed_step_id == 'val':
        safe_failed_hint = str(build_failed_step or '').strip().lower()
        if build_failed_test:
            _step_add_note('val', f'Failed test: {build_failed_test}')
        if 'solve' in safe_failed_hint:
            if build_failed_test:
                _step_add_note('val', f'Output generation failed on {build_failed_test}')
            else:
                _step_add_note('val', 'Output generation failed.')
        elif build_error_text:
            _step_add_note('val', build_error_text)
        elif error_text:
            _step_add_note('val', error_text)

    if match_total > 0:
        _step_add_fact('check', 'Matched expectations', f'{int(matched_count)}/{int(match_total)}')
    if safe_detail_status:
        _step_add_fact('check', 'Overall status', safe_detail_status.upper(), tone='ok' if safe_detail_status == 'ok' else 'danger' if safe_detail_status == 'failed' else '')
    solutions_raw = verification_details.get('solutions')
    mismatch_sources: set[str] = set()
    if isinstance(solutions_raw, list) and solutions_raw:
        solution_total = 0
        solution_matched = 0
        mismatch_lines: list[str] = []
        for item in solutions_raw:
            if not isinstance(item, dict):
                continue
            solution_total += 1
            is_matched = bool(item.get('matched'))
            if is_matched:
                solution_matched += 1
                continue
            source_path = str(item.get('source_path') or '').strip()
            source_label = Path(source_path).name if source_path else f'solution {solution_total}'
            mismatch_sources.add(source_label)
            expected_behavior = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            reason_text = _compact_error_text(str(item.get('reason') or item.get('error') or ''))
            if not reason_text:
                reason_text = _status_rule_expectation_display(expected_behavior)
            line = f'{source_label}: {reason_text}'
            mismatch_lines.append(line)
        if solution_total > 0:
            _step_add_fact('check', 'Solutions matched', f'{solution_matched}/{solution_total}')
        for line in mismatch_lines[:4]:
            _step_add_note('check', line)
        hidden_mismatch = max(0, len(mismatch_lines) - 4)
        if hidden_mismatch > 0:
            _step_add_note('check', f'+{hidden_mismatch} more mismatches')
    if error_text:
        # verification_details.error is usually the first unmatched solution hint.
        # Avoid repeating it when per-solution mismatch notes already cover that source.
        redundant_with_solution_note = False
        if mismatch_sources and ': ' in error_text:
            error_source = error_text.split(': ', 1)[0].strip()
            if error_source and (error_source in mismatch_sources):
                redundant_with_solution_note = True
        if not redundant_with_solution_note:
            _step_add_note('check', error_text)

    steps: list[dict[str, object]] = []
    for idx, token in enumerate(step_ids, start=1):
        status_token = str(status_by_step.get(token) or 'pending')
        steps.append(
            {
                'index': idx,
                'id': token,
                'title': _verification_step_title(token),
                'status': status_token,
                'status_label': _run_lifecycle_status_label(status_token),
                'detail': str(detail_by_step.get(token) or '').strip(),
                'facts': step_facts.get(token) or [],
                'notes': step_notes.get(token) or [],
            }
        )
    current_step_index, current_step_title = _run_lifecycle_current_step(steps)
    current_step_status, current_step_status_label, current_step_detail = _run_lifecycle_current_step_fields(steps, current_step_index)
    return {
        'id': 'verification',
        'title': 'Verification Progress',
        'total_steps': len(steps),
        'current_step_index': current_step_index,
        'current_step_title': current_step_title,
        'current_step_status': current_step_status,
        'current_step_status_label': current_step_status_label,
        'current_step_detail': current_step_detail,
        'summary': '',
        'steps': steps,
    }

def _run_test_count_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if bool(summary.get('execution_skipped')):
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


def _run_invocation_source_from_summary(summary: dict | None) -> str:
    block = _run_invocation_block(summary)
    return str(block.get('source') or '').strip().lower() if isinstance(block, dict) else ''


def _run_is_main_correct_invocation_source(source: object) -> bool:
    return str(source or '').strip().lower() == 'build.solve'

def _run_invocation_maps_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> dict[str, list[str]]:
    cap = max(40, int(limit))
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT ?\n        ", [int(problem_id), int(actor_user_id), cap])
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
    return invocation_to_runs


def _run_pending_invocations_from_audit(problem_id: int, actor_user_id: int, limit: int=240) -> list[dict[str, object]]:
    cap = max(40, int(limit))
    rows = config.db.fetch_all(
        """
        SELECT action,details_json,created_at
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start', 'run.cancel')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        [int(problem_id), int(actor_user_id), cap],
    )
    pending: list[dict[str, object]] = []
    seen_invocations: set[str] = set()
    for row in rows:
        details: dict = {}
        try:
            payload = json.loads(str(row["details_json"] or "{}"))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        invocation_token = _normalize_run_id_token(details.get("invocation_id"))
        if not invocation_token or invocation_token in seen_invocations:
            continue
        action_token = str(row["action"] or "").strip().lower()
        if action_token == "run.cancel":
            seen_invocations.add(invocation_token)
            continue
        status_token = str(details.get("status") or "").strip().lower()
        if status_token not in {"running", "queued"}:
            seen_invocations.add(invocation_token)
            continue
        run_ids: list[str] = []
        primary = _normalize_run_id_token(details.get("run_id"))
        if primary:
            run_ids.append(primary)
        raw_run_ids = details.get("run_ids")
        if isinstance(raw_run_ids, list):
            for item in raw_run_ids:
                token = _normalize_run_id_token(item)
                if token:
                    run_ids.append(token)
        run_ids = _dedupe_preserve_order(run_ids)
        if not run_ids:
            seen_invocations.add(invocation_token)
            continue
        source_paths: list[str] = []
        raw_submission_paths = details.get("submission_paths")
        if isinstance(raw_submission_paths, list):
            source_paths = [str(item or "").strip() for item in raw_submission_paths]
        if not source_paths:
            raw_solution_paths = details.get("solution_paths")
            if isinstance(raw_solution_paths, list):
                source_paths = [str(item or "").strip() for item in raw_solution_paths]
        pending.append(
            {
                "invocation_id": invocation_token,
                "created_at": str(row["created_at"] or "").strip(),
                "mode": str(details.get("mode") or "").strip(),
                "run_ids": run_ids,
                "source_paths": source_paths,
            }
        )
        seen_invocations.add(invocation_token)
    return pending

def _run_source_labels_from_audit(problem_id: int, actor_user_id: int, run_ids: list[str], limit: int=240) -> dict[str, str]:
    targets = {_normalize_run_id_token(token) for token in run_ids if _normalize_run_id_token(token)}
    if not targets:
        return {}
    rows = config.db.fetch_all("\n        SELECT details_json\n        FROM audit_log\n        WHERE problem_id=? AND actor_user_id=? AND action IN ('run.execute', 'verification.start')\n        ORDER BY created_at DESC\n        LIMIT ?\n        ", [int(problem_id), int(actor_user_id), max(40, int(limit))])
    resolved: dict[str, str] = {}
    for row in rows:
        if len(resolved) >= len(targets):
            break
        details: dict = {}
        try:
            payload = json.loads(str(row['details_json'] or '{}'))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        raw_run_ids = details.get('run_ids')
        if not isinstance(raw_run_ids, list):
            continue
        ordered_run_ids: list[str] = []
        for raw in raw_run_ids:
            token = _normalize_run_id_token(raw)
            if token and token not in ordered_run_ids:
                ordered_run_ids.append(token)
        if not ordered_run_ids:
            continue
        if all((run_token not in targets for run_token in ordered_run_ids)):
            continue
        path_labels: dict[str, str] = {}

        def _assign_paths(raw_paths: object) -> bool:
            if not isinstance(raw_paths, list):
                return False
            values = [str(item or '').strip() for item in raw_paths]
            if len(values) != len(ordered_run_ids):
                return False
            assigned = False
            for idx, run_token in enumerate(ordered_run_ids):
                value = str(values[idx] or '').strip()
                if not value:
                    continue
                path_labels[run_token] = value
                assigned = True
            return assigned

        has_assigned_paths = _assign_paths(details.get('submission_paths'))
        if not has_assigned_paths:
            _assign_paths(details.get('solution_paths'))
        fallback_source_label = str(details.get('source_label') or '').strip()
        fallback_upload_name = str(details.get('upload_filename') or '').strip()
        uploaded = bool(details.get('uploaded'))
        primary_run_id = _normalize_run_id_token(details.get('run_id'))
        for run_token in ordered_run_ids:
            if run_token not in targets or run_token in resolved:
                continue
            label = str(path_labels.get(run_token) or '').strip()
            if not label and primary_run_id and run_token == primary_run_id and fallback_source_label:
                label = fallback_source_label
            if not label and len(ordered_run_ids) == 1 and uploaded:
                label = fallback_upload_name or fallback_source_label or 'upload'
            if label:
                resolved[run_token] = label
    return resolved

def _run_invocation_scope_run_ids(
    problem_id: int,
    workspace_id: int,
    invocation_id: str,
    actor_user_id: int | None = None,
) -> list[str]:
    requested_token = _normalize_run_id_token(invocation_id)
    if not requested_token:
        return []
    safe_invocation_id = requested_token
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
    if actor_user_id is None:
        return []
    try:
        actor_id = int(actor_user_id)
    except Exception:
        return []
    if actor_id <= 0:
        return []
    try:
        audit_maps = _run_invocation_maps_from_audit(
            int(problem_id),
            actor_id,
            limit=max(160, int(scan_limit)),
        )
    except Exception:
        return []
    audit_run_ids = audit_maps.get(safe_invocation_id)
    if isinstance(audit_run_ids, list):
        audit_ordered = _dedupe_preserve_order(
            [_normalize_run_id_token(token) for token in audit_run_ids if _normalize_run_id_token(token)]
        )
        if audit_ordered:
            return audit_ordered
    return []

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

def _wall_time_slack_sec_for_mode(mode: object) -> int:
    token = _normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    if token == 'interactive':
        return _coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_INTERACTIVE_SEC', 15), 15, 0, 300)
    if token == 'multi-pass':
        return _coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_MULTI_PASS_SEC', 15), 15, 0, 300)
    return _coerce_int(getattr(_C, 'RUN_WALL_TIME_SLACK_PASS_FAIL_SEC', 1), 1, 0, 300)

def _effective_run_timeout_ms(time_limit_ms: int, *, mode: object='pass-fail') -> int:
    tl = max(1, int(time_limit_ms))
    slack_ms = _wall_time_slack_sec_for_mode(mode) * 1000
    return max(1, tl * 2 + slack_ms)

def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    limits = summary.get('limits')
    if isinstance(limits, dict):
        try:
            wall_ms = int(limits.get('wall_ms') or 0)
            if wall_ms > 0:
                return wall_ms
        except Exception:
            pass
        try:
            cpu_ms = int(limits.get('cpu_ms') or 0)
            if cpu_ms > 0:
                return cpu_ms
        except Exception:
            pass
    run_cfg = summary.get('run_config')
    if not isinstance(run_cfg, dict):
        return 0
    mode = _normalize_problem_mode(run_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    try:
        time_limit_ms = int(run_cfg.get('time_limit_ms') or 0)
        if time_limit_ms > 0:
            return _effective_run_timeout_ms(time_limit_ms, mode=mode)
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
    if short in {'FL', 'CE'}:
        return 'fail'
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    allowed_set = set(allowed_codes)
    required_set = set(required_codes)
    if short not in allowed_set:
        return 'fail'
    if not required_set:
        return 'ok' if short == 'AC' else 'neutral'
    if short in required_set:
        return 'ok' if short == 'AC' else 'expected-nonac'
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

def _run_cpu_wall_ms_text(cpu_ms: int, wall_ms: int) -> str:
    try:
        safe_cpu_ms = max(0, int(cpu_ms))
    except Exception:
        safe_cpu_ms = 0
    try:
        safe_wall_ms = max(0, int(wall_ms))
    except Exception:
        safe_wall_ms = safe_cpu_ms
    return f'{safe_cpu_ms}ms cpu, {safe_wall_ms}ms wall'

def _latest_iso_timestamp(values: list[str]) -> str:
    best_raw = ''
    best_ts = None
    for raw in values:
        token = str(raw or '').strip()
        if not token:
            continue
        parsed = _parse_iso_utc(token)
        if parsed is None:
            if not best_raw:
                best_raw = token
            continue
        if best_ts is None or parsed > best_ts:
            best_ts = parsed
            best_raw = token
    return best_raw

def _earliest_iso_timestamp(values: list[str]) -> str:
    best_raw = ''
    best_ts = None
    for raw in values:
        token = str(raw or '').strip()
        if not token:
            continue
        parsed = _parse_iso_utc(token)
        if parsed is None:
            if not best_raw:
                best_raw = token
            continue
        if best_ts is None or parsed < best_ts:
            best_ts = parsed
            best_raw = token
    return best_raw

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


def _run_list_hydrate_invocation_members(problem_id: int, workspace_id: int, members: list[dict[str, object]]) -> bool:
    candidate_ids: list[str] = []
    for item in members:
        if not isinstance(item, dict):
            continue
        if bool(item.get('summary_loaded')):
            continue
        run_id = _normalize_run_id_token(item.get('id'))
        if run_id:
            candidate_ids.append(run_id)
    target_ids = _dedupe_preserve_order(candidate_ids)
    if not target_ids:
        return False
    placeholders = ','.join(('?' for _ in target_ids))
    rows = config.db.fetch_all(
        f'SELECT id,status,summary_json FROM runs WHERE problem_id=? AND workspace_id=? AND id IN ({placeholders})',
        [int(problem_id), int(workspace_id), *target_ids],
    )
    row_by_id: dict[str, dict] = {}
    for row in rows:
        run_id = str(row['id'] or '').strip()
        if run_id:
            row_by_id[run_id] = row
    changed = False
    for item in members:
        if not isinstance(item, dict):
            continue
        if bool(item.get('summary_loaded')):
            continue
        run_id = _normalize_run_id_token(item.get('id'))
        if not run_id:
            item['summary_loaded'] = True
            continue
        row = row_by_id.get(run_id)
        if row is None:
            item['summary_loaded'] = True
            continue
        summary = _parse_summary_json(row['summary_json'], f'run/list/hydrate/{run_id}')
        status_text = str(row['status'] or item.get('status') or '').strip().lower() or 'unknown'
        source = _run_source_from_summary(summary)
        if not source:
            source = str(item.get('source') or '').strip()
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, status_text, summary)
        tests_total = _run_test_count_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        new_values = {
            'source': source,
            'status': status_text,
            'tests_total': tests_total,
            'expected_behavior': expected_behavior,
            'expected_behavior_label': expected_behavior_label(expected_behavior),
            'matched': bool(matched),
            'completed': bool(completed),
            'passed_all_tests': bool(observed_pass),
            'reason': str(reason or ''),
            'invocation_source': invocation_source,
            'is_main_correct_run': _run_is_main_correct_invocation_source(invocation_source),
            'summary_loaded': True,
        }
        for key, value in new_values.items():
            if item.get(key) != value:
                item[key] = value
                changed = True
    return changed

def _run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int=40, actor_user_id: int | None=None) -> list[dict]:
    limit_cap = max(1, int(limit))
    fetch_limit = limit_cap * _C.RUN_INVOCATION_LIST_SCAN_FACTOR
    rows = config.db.fetch_all('\n        SELECT id,build_id,mode,status,created_at,length(summary_json) AS summary_len\n        FROM runs\n        WHERE problem_id=? AND workspace_id=?\n        ORDER BY created_at DESC\n        LIMIT ?\n        ', [int(problem_id), int(workspace_id), fetch_limit])
    summary_budget_used = 0
    summary_rows_loaded = 0
    audit_invocation_runs_map: dict[str, list[str]] = {}
    if actor_user_id is not None:
        try:
            audit_invocation_runs_map = _run_invocation_maps_from_audit(int(problem_id), int(actor_user_id), limit=max(160, fetch_limit * 4))
        except Exception:
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
        declared_from_audit = audit_invocation_runs_map.get(invocation_id) or []
        declared_from_summary = _run_invocation_run_ids_from_summary(summary)
        declared_ids = _dedupe_preserve_order([*declared_from_audit, *declared_from_summary])
        if invocation_id not in groups:
            groups_order.append(invocation_id)
            groups[invocation_id] = {'id': invocation_id, 'created_at': row['created_at'], 'mode': str(row['mode'] or '').strip(), 'members': [], 'member_ids': set(), 'declared_run_ids': declared_ids}
        group = groups.get(invocation_id)
        if group is None:
            continue
        group_created_at = _earliest_iso_timestamp([str(group.get('created_at') or '').strip(), str(row['created_at'] or '').strip()])
        if group_created_at:
            group['created_at'] = group_created_at
        existing_declared_raw = group.get('declared_run_ids')
        existing_declared = existing_declared_raw if isinstance(existing_declared_raw, list) else []
        group['declared_run_ids'] = _dedupe_preserve_order([*existing_declared, *declared_ids])
        member_ids = group.get('member_ids')
        if isinstance(member_ids, set) and run_id in member_ids:
            continue
        if isinstance(member_ids, set):
            member_ids.add(run_id)
        expected_behavior = _run_expected_behavior_from_summary(summary, source)
        matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, status_text, summary)
        tests_total = _run_test_count_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        if isinstance(group.get('members'), list):
            group['members'].append({'id': run_id, 'source': source, 'status': status_text, 'tests_total': tests_total, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'reason': str(reason or ''), 'invocation_source': invocation_source, 'is_main_correct_run': _run_is_main_correct_invocation_source(invocation_source), 'summary_loaded': bool(summary is not None)})
    if actor_user_id is not None:
        pending_invocations = _run_pending_invocations_from_audit(
            int(problem_id),
            int(actor_user_id),
            limit=max(160, fetch_limit * 4),
        )
        for pending in reversed(pending_invocations):
            invocation_id = _normalize_run_id_token(pending.get("invocation_id"))
            if not invocation_id or invocation_id in groups:
                continue
            run_ids_raw = pending.get("run_ids")
            run_ids = run_ids_raw if isinstance(run_ids_raw, list) else []
            source_paths_raw = pending.get("source_paths")
            source_paths = source_paths_raw if isinstance(source_paths_raw, list) else []
            members: list[dict[str, object]] = []
            for idx_run, run_token in enumerate(run_ids):
                safe_run_token = _normalize_run_id_token(run_token)
                if not safe_run_token:
                    continue
                source_label = ""
                if idx_run < len(source_paths):
                    source_label = str(source_paths[idx_run] or "").strip()
                members.append(
                    {
                        "id": safe_run_token,
                        "source": source_label,
                        "status": "running",
                        "tests_total": 0,
                        "expected_behavior": "unknown",
                        "expected_behavior_label": expected_behavior_label("unknown"),
                        "matched": False,
                        "completed": False,
                        "passed_all_tests": False,
                        "reason": "pending",
                        "invocation_source": "",
                        "is_main_correct_run": False,
                        "summary_loaded": False,
                    }
                )
            if not members:
                continue
            groups_order.insert(0, invocation_id)
            groups[invocation_id] = {
                "id": invocation_id,
                "created_at": str(pending.get("created_at") or "").strip(),
                "mode": str(pending.get("mode") or "").strip(),
                "members": members,
                "member_ids": {str(item.get("id") or "") for item in members if str(item.get("id") or "").strip()},
                "declared_run_ids": [str(item.get("id") or "") for item in members if str(item.get("id") or "").strip()],
            }
    result: list[dict] = []
    for invocation_id in groups_order:
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
            ordered_members.append({'id': token, 'source': '', 'status': 'running', 'tests_total': 0, 'expected_behavior': 'unknown', 'expected_behavior_label': expected_behavior_label('unknown'), 'matched': False, 'completed': False, 'passed_all_tests': False, 'reason': 'pending'})
        if not ordered_members:
            continue
        invocation_sources = {
            str(item.get('invocation_source') or '').strip().lower()
            for item in ordered_members
            if isinstance(item, dict) and str(item.get('invocation_source') or '').strip()
        }
        is_main_correct_run = bool(invocation_sources) and invocation_sources.issubset({'build.solve'})
        safe_invocation_hint = _normalize_run_id_token(invocation_id)
        if (not is_main_correct_run) and safe_invocation_hint.startswith('inv-buildsolve-'):
            is_main_correct_run = True
        status_summary = _run_invocation_status_summary(ordered_members)
        status_text = str(status_summary['status'])
        has_running = bool(status_summary.get('has_running'))
        if (not has_running) and status_text == 'failed':
            if _run_list_hydrate_invocation_members(problem_id, workspace_id, ordered_members):
                status_summary = _run_invocation_status_summary(ordered_members)
                status_text = str(status_summary['status'])
                has_running = bool(status_summary.get('has_running'))
        running_statuses = {'running', 'queued', 'pending'}
        completed_members = [
            item
            for item in ordered_members
            if str(item.get('status') or '').strip().lower() not in running_statuses
        ]
        completed_count = len(completed_members)
        matched_completed_count = sum((1 for item in completed_members if bool(item.get('matched'))))
        matched_count = int(status_summary['matched_count'])
        total_count = int(status_summary['total_count'])
        is_failed = bool(status_summary['is_failed'])
        test_totals = [int(item.get('tests_total') or 0) for item in ordered_members if int(item.get('tests_total') or 0) > 0]
        tests_label = 'tests: -'
        tests_total = 0
        if has_running:
            if test_totals:
                tests_total = max(test_totals)
                tests_label = f'tests: up to {tests_total} (in progress)'
            else:
                tests_label = 'tests: in progress'
        elif test_totals:
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
        rejudge_context = _run_rejudge_context_for_entries(ordered_members, workspace)
        rerun_paths = rejudge_context.get('paths')
        rerun_query = str(rejudge_context.get('query') or '')
        rerun_unavailable_reason = str(rejudge_context.get('unavailable_reason') or '')
        if not isinstance(rerun_paths, list):
            rerun_paths = []
        result.append({'index': 0, 'id': invocation_id, 'run_ids': ordered_member_ids, 'run_ids_csv': ','.join(ordered_member_ids), 'run_count': total_count, 'build_id': '', 'mode': str(group.get('mode') or ''), 'status': status_text, 'status_upper': str(status_summary['status_upper']), 'created_at': group.get('created_at'), 'source_display': source_display, 'tests_label': tests_label, 'tests_total': tests_total, 'matched_count': matched_count, 'matched_completed_count': matched_completed_count, 'completed_count': completed_count, 'has_running': has_running, 'is_failed': is_failed, 'is_main_correct_run': bool(is_main_correct_run), 'rerun_solution_paths': rerun_paths, 'rerun_solution_query': rerun_query, 'rerun_unavailable_reason': rerun_unavailable_reason})
    def _invocation_run_time_sort_key(item: dict[str, object]) -> tuple[int, float, str]:
        raw = str(item.get('created_at') or '').strip()
        parsed = _parse_iso_utc(raw)
        if parsed is None:
            return (0, -1.0, raw)
        return (1, float(parsed.timestamp()), raw)

    result.sort(key=_invocation_run_time_sort_key, reverse=True)
    trimmed = result[:limit_cap]
    for idx, row in enumerate(trimmed, start=1):
        row['index'] = idx
    return trimmed

def _build_run_detail_context(
    ctx: dict,
    run_ids: list[str],
    execute_mode: str,
    *,
    requested_invocation_id: str = '',
    include_row_details: bool = False,
    detail_test_name: str = '',
) -> dict:
    workspace = Path(ctx['workspace']['path'])
    workspace_id = int(ctx['workspace']['id'])
    problem_id = int(ctx['problem']['id'])
    actor_user_id = int(ctx['user']['id'])
    problem_slug = str(ctx.get('problem', {}).get('slug') or '').strip()
    username = str(ctx.get('user', {}).get('username') or '').strip()
    fallback_timeout_ms = 0
    try:
        _payload, general_cfg, _cfg_path = _read_problem_config(workspace)
        fallback_timeout_ms = _effective_run_timeout_ms(
            int(general_cfg.get('time_limit_ms') or _C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']),
            mode=general_cfg.get('mode'),
        )
    except Exception:
        fallback_timeout_ms = 0
    selected_ids = [token for token in run_ids if token]
    rows_by_id: dict[str, dict] = {}
    if selected_ids:
        placeholders = ','.join(('?' for _ in selected_ids))
        rows = config.db.fetch_all(f'\n            SELECT id,build_id,mode,status,summary_json,created_at,finished_at\n            FROM runs\n            WHERE workspace_id=? AND id IN ({placeholders})\n            ', [workspace_id, *selected_ids])
        for row in rows:
            run_id = str(row['id'] or '').strip()
            if run_id:
                rows_by_id[run_id] = dict(row)
    audit_source_labels: dict[str, str] = {}
    try:
        audit_source_labels = _run_source_labels_from_audit(problem_id, actor_user_id, selected_ids, limit=max(240, len(selected_ids) * 8))
    except Exception:
        audit_source_labels = {}
    invocation_id_hint = _normalize_run_id_token(requested_invocation_id)
    verification_audit_row: dict[str, object] = {}
    verification_details: dict[str, object] = {}
    if invocation_id_hint:
        verification_audit_row = _run_verification_details_from_audit(problem_id, actor_user_id, invocation_id_hint)
        details_obj = verification_audit_row.get('details')
        if isinstance(details_obj, dict):
            verification_details = details_obj
    invocation_created_at = str(verification_audit_row.get('created_at') or '').strip() if isinstance(verification_audit_row, dict) else ''
    expected_by_run_id: dict[str, str] = {}
    expected_by_source: dict[str, str] = {}
    solutions_raw = verification_details.get('solutions')
    if isinstance(solutions_raw, list):
        for item in solutions_raw:
            if not isinstance(item, dict):
                continue
            expected_token = normalize_expected_behavior(str(item.get('expected_behavior') or 'unknown'))
            if expected_token == 'unknown':
                continue
            run_token = _normalize_run_id_token(item.get('run_id'))
            if run_token and run_token not in expected_by_run_id:
                expected_by_run_id[run_token] = expected_token
            source_token = _normalize_optional_component_source_path_safe(
                str(item.get('source_path') or ''),
                'solutions',
                'solution path',
            )
            if source_token and source_token not in expected_by_source:
                expected_by_source[source_token] = expected_token
    expected_by_source_cache: dict[str, str] = dict(expected_by_source)

    def _expected_from_workspace_source(source_rel: str) -> str:
        safe_source = _normalize_optional_component_source_path_safe(
            source_rel,
            'solutions',
            'solution path',
        )
        if not safe_source:
            return ''
        cached = normalize_expected_behavior(str(expected_by_source_cache.get(safe_source) or 'unknown'))
        if cached != 'unknown':
            return cached
        expected_token = 'unknown'
        try:
            entry = _solution_metadata_entry(workspace, safe_source)
            expected_token = normalize_expected_behavior(str(entry.get('expected_behavior') or 'unknown'))
        except Exception:
            expected_token = normalize_expected_behavior(infer_expected_behavior_from_name(safe_source))
        if expected_token != 'unknown':
            expected_by_source_cache[safe_source] = expected_token
            return expected_token
        return ''
    columns: list[dict] = []
    all_tests: set[str] = set()
    selected_test_name_hint = _normalize_run_test_name_token(detail_test_name) if include_row_details else ''
    domjudge_case_cells_by_run = _run_domjudge_case_cells(selected_ids)
    for run_id in selected_ids:
        row = rows_by_id.get(run_id)
        status = 'running'
        mode = execute_mode
        created_at = invocation_created_at
        finished_at = ''
        build_id = ''
        summary_raw = None
        if row is not None:
            status = str(row.get('status') or '').strip().lower() or status
            mode = str(row.get('mode') or '').strip() or mode
            created_at = row.get('created_at') or created_at
            finished_at = str(row.get('finished_at') or '').strip()
            build_id = str(row.get('build_id') or '').strip()
            summary_raw = row.get('summary_json')
        summary = _parse_summary_json(summary_raw, f'run/{run_id}') if summary_raw else None
        if isinstance(summary, dict):
            _cap_summary_list(summary, 'tests', _C.RUN_DETAIL_TEST_LIST_LIMIT, 'tests_truncated', 'tests_total', 'tests_limit')
            _cap_summary_list(summary, 'compile_diagnostics', _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT, 'compile_diagnostics_truncated', 'compile_diagnostics_total', 'compile_diagnostics_limit')
            if include_row_details:
                _cap_run_test_feedback_files(summary, _C.RUN_TEST_FEEDBACK_FILE_LIST_LIMIT)
            compile_diags = summary.get('compile_diagnostics')
            if isinstance(compile_diags, list):
                normalized_diags = _normalize_diagnostics(compile_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                summary['compile_diagnostics'] = _decorate_compile_diagnostics(normalized_diags)
        source = _run_source_from_summary(summary)
        invocation_source = _run_invocation_source_from_summary(summary)
        is_main_correct_run = _run_is_main_correct_invocation_source(invocation_source)
        audit_source_label = str(audit_source_labels.get(run_id) or '').strip()
        source_for_display = source or audit_source_label
        title = Path(source_for_display).name if source_for_display else ''
        if not title:
            title = run_id or 'unknown run'
        source_href = ''
        source_rel = _normalize_workspace_rel_path(source_for_display)
        if problem_slug and username and source_rel and _workspace_rel_file_exists(workspace, source_rel):
            safe_solution = _normalize_optional_component_source_path_safe(source_rel, 'solutions', 'solution path')
            if safe_solution:
                source_href = f'/problems/{problem_slug}/{username}/solutions/editor?path={quote_plus(safe_solution)}'
            else:
                source_href = f'/problems/{problem_slug}/{username}/files?path={quote_plus(source_rel)}&src=run'
        expected_behavior = _run_expected_behavior_from_summary(summary, source_for_display)
        if expected_behavior == 'unknown':
            mapped_expected = expected_by_run_id.get(run_id)
            if not mapped_expected and source_rel:
                mapped_expected = expected_by_source.get(source_rel)
            if not mapped_expected and source_rel:
                mapped_expected = _expected_from_workspace_source(source_rel)
            if mapped_expected:
                expected_behavior = mapped_expected
        matched, completed, observed_pass, match_reason = _verification_solution_match(expected_behavior, status, summary)
        _, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
        expected_display = _status_rule_expected_display(expected_behavior)
        expected_is_ac_only = bool(required_codes == ('AC',) and allowed_codes == ('AC',))
        got_short = _run_actual_short(status, summary)
        got_display = _run_actual_display(status, summary)
        expected_mismatch = bool(completed and (not matched))
        execution_skipped_from_summary = False
        if isinstance(summary, dict):
            execution_skipped_from_summary = bool(summary.get('execution_skipped'))
            if not execution_skipped_from_summary and str(summary.get('failure_stage') or '').strip().lower() == 'build':
                execution_skipped_from_summary = True
        tests_map: dict[str, dict] = {}
        max_time_ms = 0
        max_memory_kb = 0
        has_test_metrics = False
        tests_raw = (summary.get('tests') if isinstance(summary, dict) else None) if not execution_skipped_from_summary else None
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
                if selected_test_name_hint and test_name != selected_test_name_hint:
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
                    time_user_ms = int(item.get('time_user_ms', time_ms) or 0)
                except Exception:
                    time_user_ms = time_ms
                if str(verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (time_user_ms > timeout_limit_ms):
                    time_user_ms = timeout_limit_ms
                try:
                    time_wall_ms = int(item.get('time_wall_ms', time_user_ms) or 0)
                except Exception:
                    time_wall_ms = time_user_ms
                try:
                    memory_kb = int(item.get('memory_kb') or 0)
                except Exception:
                    memory_kb = 0
                memory_mb_text = _run_memory_mb_text(memory_kb)
                has_test_metrics = True
                if time_ms > max_time_ms:
                    max_time_ms = time_ms
                if memory_kb > max_memory_kb:
                    max_memory_kb = memory_kb
                detail_payload: dict[str, object] | None = None
                if include_row_details:
                    passes_raw = item.get('passes')
                    test_stem = Path(test_name).stem
                    feedback_display = '-'
                    inline_feedback = _compact_error_text(str(item.get('message') or item.get('error') or ''))
                    feedback_files_raw = item.get('feedback_files')
                    feedback_items: list[str] = []
                    if isinstance(feedback_files_raw, list):
                        for feedback_entry in feedback_files_raw:
                            token = str(feedback_entry or '').strip()
                            if token:
                                feedback_items.append(token)
                    if inline_feedback:
                        feedback_display = inline_feedback
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
                        if hidden_count > 0 and feedback_display != '-':
                            feedback_display = f'{feedback_display} (+{hidden_count} more)' if feedback_display != '-' else f'+{_count_label(hidden_count, "file")}'
                    pass_rows: list[dict[str, str]] = []
                    if isinstance(passes_raw, list) and passes_raw:
                        for pass_item in passes_raw:
                            if not isinstance(pass_item, dict):
                                continue
                            pass_verdict = str(pass_item.get('verdict') or '').strip().upper() or '-'
                            pass_verdict_short = _run_verdict_short(pass_verdict)
                            try:
                                pass_time_user_ms = int(pass_item.get('time_user_ms', pass_item.get('time_ms', 0)) or 0)
                            except Exception:
                                pass_time_user_ms = 0
                            if str(pass_verdict or '').strip().upper().startswith('TL') and timeout_limit_ms > 0 and (pass_time_user_ms > timeout_limit_ms):
                                pass_time_user_ms = timeout_limit_ms
                            try:
                                pass_time_wall_ms = int(pass_item.get('time_wall_ms', pass_time_user_ms) or 0)
                            except Exception:
                                pass_time_wall_ms = pass_time_user_ms
                            try:
                                pass_memory_kb = int(pass_item.get('memory_kb') or 0)
                            except Exception:
                                pass_memory_kb = 0
                            pass_feedback = _compact_error_text(str(pass_item.get('feedback') or pass_item.get('message') or ''))
                            row_feedback_display = pass_feedback or feedback_display
                            output_rel = f'{test_stem}.out' if test_stem else ''
                            checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                            feedback_rel = ''
                            if feedback_items:
                                feedback_rel = str(feedback_items[0] or '').strip()
                            pass_rows.append({'pass_label': '-', 'verdict_short': pass_verdict_short, 'kind': _run_cell_kind(pass_verdict, expected_behavior), 'time_display': _run_cpu_wall_ms_text(pass_time_user_ms, pass_time_wall_ms), 'memory_display': _run_memory_mb_text(pass_memory_kb), 'feedback_display': row_feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    if not pass_rows:
                        output_rel = f'{test_stem}.out' if test_stem else ''
                        checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                        feedback_rel = ''
                        if feedback_items:
                            feedback_rel = str(feedback_items[0] or '').strip()
                        pass_rows.append({'pass_label': '-', 'verdict_short': verdict_short, 'kind': _run_cell_kind(verdict, expected_behavior), 'time_display': _run_cpu_wall_ms_text(time_user_ms, time_wall_ms), 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'output_rel': output_rel, 'checker_log_rel': checker_log_rel, 'feedback_rel': feedback_rel})
                    final_row = dict(pass_rows[-1]) if pass_rows else {}
                    for candidate in reversed(pass_rows):
                        verdict_token = str(candidate.get('verdict_short') or '').strip()
                        if verdict_token and verdict_token not in {'--', '-'}:
                            final_row = dict(candidate)
                            break
                    detail_payload = {'verdict': verdict, 'verdict_short': verdict_short, 'time_display': f'{time_ms}ms', 'memory_display': memory_mb_text, 'feedback_display': feedback_display, 'pass_rows': pass_rows, 'final_row': final_row}
                all_tests.add(test_name)
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': verdict_short,
                    'short': verdict_short,
                    'metrics': f'{time_ms}ms/{memory_mb_text}',
                    'kind': _run_cell_kind(verdict, expected_behavior),
                    'detail': detail_payload,
                    'detail_available': True,
                }
        execution_skipped = bool(execution_skipped_from_summary)
        execution_skipped_reason = ''
        if isinstance(summary, dict):
            execution_skipped_reason = _compact_error_text(str(summary.get('execution_skipped_reason') or summary.get('error') or ''))
        if not execution_skipped:
            case_cells = domjudge_case_cells_by_run.get(run_id) or {}
            for test_name, case_cell in case_cells.items():
                if selected_test_name_hint and test_name != selected_test_name_hint:
                    continue
                all_tests.add(test_name)
                current_cell = tests_map.get(test_name) if isinstance(tests_map.get(test_name), dict) else None
                current_short = str(current_cell.get('short') or '').strip().upper() if isinstance(current_cell, dict) else ''
                current_has_verdict = bool(current_short and current_short not in {'--', '..'})
                if current_has_verdict:
                    continue
                verdict = str(case_cell.get('verdict') or '').strip().upper()
                short = str(case_cell.get('short') or '..').strip().upper() or '..'
                try:
                    time_ms = max(0, int(case_cell.get('time_ms') or 0))
                except Exception:
                    time_ms = 0
                try:
                    memory_kb = max(0, int(case_cell.get('memory_kb') or 0))
                except Exception:
                    memory_kb = 0
                metrics = str(case_cell.get('metrics') or '-').strip() or '-'
                detail_payload = None
                detail_available = False
                if bool(case_cell.get('reported')):
                    test_stem = Path(test_name).stem
                    output_rel = f'{test_stem}.out' if test_stem else ''
                    checker_log_rel = f'feedback_dir/{test_stem}/checker.log' if test_stem else ''
                    try:
                        case_cpu_ms = max(0, int(case_cell.get('cpu_ms') or time_ms))
                    except Exception:
                        case_cpu_ms = time_ms
                    try:
                        case_wall_ms = max(case_cpu_ms, int(case_cell.get('wall_ms') or case_cpu_ms))
                    except Exception:
                        case_wall_ms = case_cpu_ms
                    pass_row = {
                        'pass_label': '-',
                        'verdict_short': short if short else '--',
                        'kind': _run_cell_kind(verdict, expected_behavior),
                        'time_display': _run_cpu_wall_ms_text(case_cpu_ms, case_wall_ms),
                        'memory_display': _run_memory_mb_text(memory_kb),
                        'feedback_display': '-',
                        'output_rel': output_rel,
                        'checker_log_rel': checker_log_rel,
                        'feedback_rel': '',
                    }
                    detail_payload = {
                        'verdict': verdict or '-',
                        'verdict_short': short if short else '--',
                        'time_display': f'{time_ms}ms',
                        'memory_display': _run_memory_mb_text(memory_kb),
                        'feedback_display': '-',
                        'pass_rows': [pass_row],
                        'final_row': dict(pass_row),
                    }
                    detail_available = True
                tests_map[test_name] = {
                    'verdict': verdict,
                    'time_ms': time_ms,
                    'memory_kb': memory_kb,
                    'text': short,
                    'short': short,
                    'metrics': metrics,
                    'kind': _run_cell_kind(verdict, expected_behavior) if verdict else 'neutral',
                    'detail': detail_payload,
                    'detail_available': bool(detail_available),
                }
                if bool(case_cell.get('reported')):
                    has_test_metrics = True
                    if time_ms > max_time_ms:
                        max_time_ms = time_ms
                    if memory_kb > max_memory_kb:
                        max_memory_kb = memory_kb
        max_time_display = f'{max_time_ms}ms' if has_test_metrics else '-'
        max_memory_display = _run_memory_mb_text(max_memory_kb) if has_test_metrics else '-'
        columns.append({'id': run_id, 'build_id': build_id, 'title': title, 'source': source_for_display or '-', 'source_href': source_href, 'invocation_source': invocation_source, 'is_main_correct_run': bool(is_main_correct_run), 'status': status, 'status_upper': status.upper(), 'mode': mode, 'created_at': created_at, 'finished_at': finished_at, 'summary': summary, 'has_run_row': bool(row is not None), 'tests_map': tests_map, 'compile_log': str(summary.get('compile_log') or '') if isinstance(summary, dict) else '', 'compile_diagnostics': summary.get('compile_diagnostics') if isinstance(summary, dict) else [], 'compile_diagnostics_truncated': bool(summary.get('compile_diagnostics_truncated')) if isinstance(summary, dict) else False, 'compile_diagnostics_total': int(summary.get('compile_diagnostics_total') or 0) if isinstance(summary, dict) else 0, 'compile_diagnostics_limit': int(summary.get('compile_diagnostics_limit') or 0) if isinstance(summary, dict) else 0, 'error': str(summary.get('error') or '') if isinstance(summary, dict) else '', 'error_display': _run_error_display(str(summary.get('error') or '')) if isinstance(summary, dict) else '', 'tests_total': int(summary.get('tests_total') or len(tests_map)) if isinstance(summary, dict) else len(tests_map), 'tests_truncated': bool(summary.get('tests_truncated')) if isinstance(summary, dict) else False, 'expected_behavior': expected_behavior, 'expected_behavior_label': expected_behavior_label(expected_behavior), 'expected_display': expected_display, 'expected_is_ac_only': bool(expected_is_ac_only), 'got_short': got_short, 'got_display': got_display, 'expected_mismatch': bool(expected_mismatch), 'matched': bool(matched), 'completed': bool(completed), 'passed_all_tests': bool(observed_pass), 'match_reason': str(match_reason or ''), 'execution_skipped': bool(execution_skipped), 'execution_skipped_reason': execution_skipped_reason, 'max_time_ms': int(max_time_ms), 'max_time_display': max_time_display, 'max_memory_kb': int(max_memory_kb), 'max_memory_display': max_memory_display})
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    row_index_by_test = {name: idx for idx, name in enumerate(ordered_tests, start=1)}
    detail_rows: list[dict] = []
    if not include_row_details:
        for idx, test_name in enumerate(ordered_tests, start=1):
            cells: list[dict] = []
            has_detail = False
            for col in columns:
                cell = col['tests_map'].get(test_name)
                if cell is None:
                    cells.append({'text': '--', 'short': '--', 'metrics': '-', 'kind': 'neutral', 'detail': None})
                    continue
                if bool(cell.get('detail_available')):
                    has_detail = True
                cells.append(
                    {
                        'text': str(cell.get('text') or '--'),
                        'short': str(cell.get('short') or cell.get('text') or '--'),
                        'metrics': str(cell.get('metrics') or '-'),
                        'kind': str(cell.get('kind') or 'neutral'),
                        'detail': None,
                    }
                )
            detail_rows.append(
                {
                    'index': idx,
                    'test_name': test_name,
                    'row_id': f'test-detail-{idx}',
                    'cells': cells,
                    'has_detail': bool(has_detail),
                }
            )
    else:
        selected_test_name = selected_test_name_hint
        target_tests = ordered_tests
        if selected_test_name:
            target_tests = [name for name in ordered_tests if name == selected_test_name]

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

        def _workspace_answer_preview(test_name: str) -> dict[str, object]:
            if not problem_slug or not username:
                return _run_detail_preview_unavailable('missing')
            test_stem = Path(str(test_name or '').strip()).stem
            if not test_stem:
                return _run_detail_preview_unavailable('missing')
            answer_source_rel = f'tests/answers/{test_stem}.ans'
            try:
                preview_file = _safe_workspace_path(workspace, answer_source_rel)
            except HTTPException:
                return _run_detail_preview_unavailable('missing')
            if (not preview_file.exists()) or (not preview_file.is_file()) or preview_file.is_symlink():
                return _run_detail_preview_unavailable('missing')
            download_href = f'/problems/{problem_slug}/{username}/files/download?path={quote_plus(answer_source_rel)}&src=workspace'
            return _run_detail_preview_from_path(preview_file, download_href)

        for test_name in target_tests:
            row_index = int(row_index_by_test.get(test_name) or 0)
            if row_index <= 0:
                continue
            input_rel = f'tests/{test_name}'
            answer_name = _run_test_answer_name(test_name)
            answer_rel = f'ans/{answer_name}' if answer_name else ''
            input_preview = _run_detail_preview_unavailable('missing')
            answer_preview = _run_detail_preview_unavailable('missing')
            source_answer_preview = _workspace_answer_preview(test_name)
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
            if bool(source_answer_preview.get('available')) and _run_detail_preview_is_noise(answer_preview):
                answer_preview = source_answer_preview
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
                            feedback_rel = str(row_payload.get('feedback_rel') or '').strip()
                            feedback_preview = _run_detail_preview_unavailable('missing')
                            if feedback_rel:
                                feedback_preview = _run_artifact_preview(str(col.get('id') or ''), feedback_rel)
                            elif checker_log_rel:
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
                    final_row_raw = detail_payload.get('final_row')
                    final_row_payload = dict(final_row_raw) if isinstance(final_row_raw, dict) else {}
                    if pass_rows_payload:
                        final_row_payload = dict(pass_rows_payload[-1])
                        for candidate in reversed(pass_rows_payload):
                            verdict_token = str(candidate.get('verdict_short') or '').strip()
                            if verdict_token and verdict_token not in {'--', '-'}:
                                final_row_payload = dict(candidate)
                                break
                    feedback_token = str(final_row_payload.get('feedback_display') or '-').strip()
                    feedback_preview_obj = final_row_payload.get('feedback_preview')
                    if (not feedback_token) or feedback_token == '-' or feedback_token.startswith('feedback_dir/'):
                        if isinstance(feedback_preview_obj, dict) and bool(feedback_preview_obj.get('available')):
                            preview_text = str(feedback_preview_obj.get('text') or '').replace('\r\n', '\n').replace('\r', '\n')
                            first_line = ''
                            for raw_line in preview_text.splitlines():
                                line = str(raw_line or '').strip()
                                if line:
                                    first_line = line
                                    break
                            if first_line:
                                if len(first_line) > 160:
                                    first_line = first_line[:157].rstrip() + '...'
                                feedback_token = first_line
                    if not feedback_token or feedback_token.startswith('feedback_dir/'):
                        feedback_token = '-'
                    final_row_payload['feedback_display'] = feedback_token
                    output_preview_obj = final_row_payload.get('output_preview')
                    interactive_mode = str(col.get('mode') or '').strip().lower() in {'interactive', 'multi-pass'}
                    if interactive_mode and isinstance(output_preview_obj, dict):
                        final_row_payload['interactive_transcript'] = _interactive_transcript_preview(output_preview_obj)
                    detail_payload['final_row'] = final_row_payload
                cells.append({'text': str(cell['text']), 'short': str(cell.get('short') or cell.get('text') or '--'), 'metrics': str(cell.get('metrics') or '-'), 'kind': str(cell['kind']), 'detail': detail_payload})
            detail_rows.append(
                {
                    'index': row_index,
                    'test_name': test_name,
                    'row_id': f'test-detail-{row_index}',
                    'input_preview': input_preview,
                    'answer_preview': answer_preview,
                    'cells': cells,
                    'has_detail': any((cell.get('detail') is not None for cell in cells)),
                }
            )
    status_summary = _run_invocation_status_summary(columns)
    detail_invocation_sources = {
        str(col.get('invocation_source') or '').strip().lower()
        for col in columns
        if isinstance(col, dict) and str(col.get('invocation_source') or '').strip()
    }
    detail_is_main_correct_run = bool(detail_invocation_sources) and detail_invocation_sources.issubset({'build.solve'})
    if (not detail_is_main_correct_run) and isinstance(verification_details, dict):
        details_source = str(verification_details.get('source') or '').strip().lower()
        if details_source == 'build.solve':
            detail_is_main_correct_run = True
    if (not detail_is_main_correct_run) and isinstance(verification_details, dict):
        build_status_token = str(verification_details.get('build_status') or '').strip().lower()
        has_materialized_summary = any(
            isinstance(col, dict) and isinstance(col.get('summary'), dict)
            for col in columns
        )
        if (build_status_token in {'running', 'queued', 'pending'}) and (not has_materialized_summary):
            detail_is_main_correct_run = True
    safe_invocation_hint = _normalize_run_id_token(invocation_id_hint)
    if (not detail_is_main_correct_run) and safe_invocation_hint.startswith('inv-buildsolve-'):
        detail_is_main_correct_run = True
    rejudge_context = _run_rejudge_context_for_entries(columns, workspace)
    rerun_paths = rejudge_context.get('paths')
    if not isinstance(rerun_paths, list):
        rerun_paths = []
    progress_total = 0
    for col in columns:
        if bool(col.get('execution_skipped')):
            continue
        try:
            progress_total = max(progress_total, int(col.get('tests_total') or 0))
        except Exception:
            continue
    progress_reported = len(ordered_tests)
    progress_placeholder_total = min(progress_total, 24) if bool(status_summary['has_running']) and progress_total > 0 else 0
    last_updated_candidates: list[str] = [str(col.get('finished_at') or '').strip() for col in columns]
    last_updated_candidates.extend([str(col.get('created_at') or '').strip() for col in columns])
    if invocation_created_at:
        last_updated_candidates.append(invocation_created_at)
    last_updated = _latest_iso_timestamp(last_updated_candidates)
    invocation_id = invocation_id_hint
    for col in columns:
        summary_obj = col.get('summary')
        token = _run_invocation_id_from_summary(summary_obj if isinstance(summary_obj, dict) else None, '')
        if token:
            invocation_id = token
            break
    lifecycle_cards: list[dict[str, object]] = []
    if (not verification_details) and invocation_id:
        verification_audit_row = _run_verification_details_from_audit(problem_id, actor_user_id, invocation_id)
        details_obj = verification_audit_row.get('details')
        verification_details = details_obj if isinstance(details_obj, dict) else {}
    if selected_ids:
        lifecycle_cards = [
            _build_verification_lifecycle_card(
                problem_slug=str(ctx['problem']['slug']),
                problem_id=int(ctx['problem']['id']),
                workspace_id=int(ctx['workspace']['id']),
                actor_user_id=int(ctx['user']['id']),
                invocation_id=invocation_id,
                verification_details=verification_details,
                columns=columns,
                detail_status=str(status_summary['status']),
                detail_running=bool(status_summary['has_running']),
                progress_reported=progress_reported,
                progress_total=progress_total,
                matched_count=int(status_summary['matched_count']),
                match_total=int(status_summary['total_count']),
            )
        ]
    verification_build: dict[str, object] = {
        'available': False,
        'build_id': '',
        'status': '',
        'error': '',
        'error_display': '',
        'log_rows': [],
        'diagnostics': [],
        'diagnostics_total': 0,
        'diagnostics_truncated': False,
        'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
    }
    try:
        build_id = str(verification_details.get('build_id') or '').strip() if isinstance(verification_details, dict) else ''
        safe_build_id = build_id if is_canonical_artifact_id(build_id) else ''
        if safe_build_id and problem_slug and username:
            build_row = config.db.fetch_one(
                'SELECT status,summary_json FROM builds WHERE id=? AND problem_id=?',
                [safe_build_id, problem_id],
            )
            build_summary = _parse_summary_json(build_row['summary_json'], f'build/{safe_build_id}') if build_row is not None else {}
            build_status = str(build_row['status'] or '').strip().lower() if build_row is not None else str(verification_details.get('build_status') or '').strip().lower()
            build_error = str(verification_details.get('build_error') or '').strip()
            if not build_error and isinstance(build_summary, dict):
                build_error = str(build_summary.get('error') or '').strip()
            if not build_error:
                build_error = str(verification_details.get('error') or '').strip()
            log_rows: list[dict[str, str]] = []
            for name in ('failure.log', 'compile.log', 'generate.log', 'validate.log', 'solve.log'):
                rel = f'logs/{name}'
                try:
                    _safe_artifact_path(problem_slug, safe_build_id, rel)
                except HTTPException:
                    continue
                log_rows.append(
                    {
                        'name': name,
                        'href': f'/problems/{problem_slug}/{username}/artifacts/{safe_build_id}/{rel}',
                    }
                )
            diagnostics_rows: list[dict[str, object]] = []
            diagnostics_total = 0
            diagnostics_truncated = False
            if isinstance(build_summary, dict):
                raw_diags = build_summary.get('diagnostics')
                if isinstance(raw_diags, list):
                    diagnostics_total = len(raw_diags)
                    capped_diags = raw_diags[: _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT]
                    diagnostics_truncated = diagnostics_total > len(capped_diags)
                    normalized_diags = _normalize_diagnostics(capped_diags, _C.DIAGNOSTIC_MESSAGE_CHAR_LIMIT)
                    diagnostics_rows = _decorate_compile_diagnostics(normalized_diags)
            verification_build = {
                'available': True,
                'build_id': safe_build_id,
                'status': build_status,
                'error': build_error,
                'error_display': _run_error_display(build_error),
                'log_rows': log_rows,
                'diagnostics': diagnostics_rows,
                'diagnostics_total': diagnostics_total,
                'diagnostics_truncated': diagnostics_truncated,
                'diagnostics_limit': _C.RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT,
            }
    except Exception:
        pass

    return {
        'detail_columns': columns,
        'detail_rows': detail_rows,
        'selected_run_ids': selected_ids,
        'rerun_solution_paths': rerun_paths,
        'rerun_solution_query': str(rejudge_context.get('query') or ''),
        'rerun_unavailable_reason': str(rejudge_context.get('unavailable_reason') or ''),
        'matched_count': int(status_summary['matched_count']),
        'match_total': int(status_summary['total_count']),
        'all_matched': bool(columns) and all((bool(col.get('matched')) for col in columns)),
        'detail_status': str(status_summary['status']),
        'detail_status_upper': str(status_summary['status_upper']),
        'detail_is_main_correct_run': bool(detail_is_main_correct_run),
        'detail_running': bool(status_summary['has_running']),
        'detail_last_updated': last_updated,
        'detail_progress_total': progress_total,
        'detail_progress_reported': progress_reported,
        'detail_progress_placeholder_total': progress_placeholder_total,
        'detail_lifecycle_cards': lifecycle_cards,
        'detail_verification_build': verification_build,
    }
