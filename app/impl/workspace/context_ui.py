from __future__ import annotations
from app.impl.auth.session import require_session_user
from pathlib import Path
from typing import Annotated, TypedDict, cast

from fastapi import HTTPException, Request, Depends

from app.impl.auth.session import has_sudo_session
from app.impl.auth.shared import template_response
from app.impl.runtime.config import config

from app.main_util import normalize_workspace_rel_path
from app.service.statement.constant import (
    STATEMENT_SECTIONS_DIR,
    is_ignored_statement_section_entry,
)
from app.service.statement.context import statement_languages
from app.service.statement.render import default_statement_title_for_workspace
from app.service.verification.runtime import coerce_int, normalize_pass_limit, normalize_problem_mode

from .access import (
    problem_acl_entries,
    require_read_access,
    workspace_access_context,
)
from .artifact import artifact_version_number
from .context import count_label
from .context_operation import (
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
from .problem_config import read_problem_config
from app.service.repository.revision import git_commit_count, workspace_revision_info

_C = config.constants


class SystemLimitRow(TypedDict):
    label: str
    value: str


class SystemLimitInfo(TypedDict):
    title: str
    description: str
    rows: list[SystemLimitRow]


def _system_limit_info() -> SystemLimitInfo:
    return {
        'title': 'System limits',
        'description': 'Contact an administrator to change these limits if needed.',
        'rows': [
            {'label': 'Program input/output limit', 'value': f'{int(_C.RUN_EXEC_OUTPUT_KB)} KiB'},
            {'label': 'Compilation size limit', 'value': f'{int(_C.TOOLCHAIN_COMPILE_OUTPUT_KB)} KiB'},
            {'label': 'Saved judging log limit', 'value': f'{int(_C.JUDGEHOST_STORED_LOG_LIMIT_BYTES)} bytes'},
        ],
    }


def page_ctx(problem: str, user: str, include_branches: bool=True, refresh_status: bool=True, include_recent: bool=True, include_workspace_changes: bool=True) -> dict:
    _ = include_branches
    try:
        problem_id, user_id = config.workspace_service.page_identity(problem, user)
        access = workspace_access_context(problem_id, user_id)
        require_read_access({'access': access})
        if refresh_status:
            # Provision without the lock-side refresh; the explicit refresh below updates DB once.
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
        live_status: dict[str, object] | None = None
        try:
            live_status = cast(
                dict[str, object],
                config.workspace_service.refresh_workspace_status_with_ids(
                    workspace_path,
                    int(ctx['problem']['id']),
                    int(ctx['user']['id']),
                ),
            )
        except Exception:
            live_status = None
        if live_status is not None:
            branch_raw = cast(str | None, live_status.get('branch'))
            ctx['workspace']['branch'] = branch_raw or 'main'
            head_commit_raw = cast(str | None, live_status.get('head_commit'))
            ctx['workspace']['head_commit'] = head_commit_raw or ''
            ctx['workspace']['dirty'] = 1 if bool(live_status.get('dirty')) else 0
    branch_raw = cast(str | None, ctx['workspace'].get('branch'))
    workspace_branch = branch_raw or 'main'
    workspace_head_raw = cast(str | None, ctx['workspace'].get('head_commit'))
    workspace_head = workspace_head_raw or ''
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    _payload, general_cfg, _cfg_path = read_problem_config(workspace_path)
    safe_mode = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    ctx['problem_mode'] = safe_mode
    ctx['general_cfg'] = {'time_limit_ms': coerce_int(general_cfg.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS), 'memory_limit_mb': coerce_int(general_cfg.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB), 'mode': safe_mode, 'pass_limit': normalize_pass_limit(general_cfg.get('pass_limit'), int(_C.GENERAL_CONFIG_DEFAULTS['pass_limit']))}
    ctx['system_limit_info'] = _system_limit_info()
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
    upstream_higher = bool(ctx['workspace_revision'].get('upstream_higher'))
    ctx['workspace_needs_update'] = True if upstream_higher else behind_count > 0
    ctx['head_short'] = workspace_head[:8]
    try:
        ctx['checker_status'] = checker_status_context(workspace_path)
    except Exception:
        ctx['checker_status'] = {'mode': 'missing', 'display': 'unknown', 'standard_checker': '', 'standard_expected_checker': '', 'standard_warning': '', 'standard_valid': False, 'repo_source': 'checkers/checker.cpp', 'repo_source_exists': False}
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
            'verification_id': '',
            'error': '',
            'created_at': '',
            'stale': False,
            'stale_reason': '',
        }
    latest_verification = ctx.get('latest_artifact_verification')
    ctx['latest_verification_version'] = artifact_version_number(latest_verification['id']) if latest_verification else None
    ctx['nav_status'] = _build_problem_nav_status(ctx)
    return ctx

def _build_problem_nav_status(ctx: dict) -> dict[str, dict[str, object]]:

    def _to_int(value: object, default: int=0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _row_value(row: dict[str, object] | None, key: str, default: object='') -> object:
        if row is None:
            return default
        return row.get(key, default)

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

    def _statement_seed_defaults(workspace: Path) -> dict[str, str]:
        return {
            'name.tex': default_statement_title_for_workspace(workspace) + '\n',
            'legend.tex': '',
            'input.tex': '',
            'output.tex': '',
            'interaction.tex': '',
            'notes.tex': '',
        }

    def _read_optional_text(path: Path, fallback: str) -> str:
        try:
            if path.exists() and path.is_file() and (not path.is_symlink()):
                return path.read_text(encoding='utf-8')
        except OSError:
            return fallback
        return fallback

    def _statement_is_initial_empty(workspace: Path, languages: list[str]) -> bool:
        if not languages:
            return True
        if len(languages) != 1:
            return False
        language = languages[0]
        section_root = workspace / STATEMENT_SECTIONS_DIR / language
        if not section_root.exists() or (not section_root.is_dir()) or section_root.is_symlink():
            return True
        seed_defaults = _statement_seed_defaults(workspace)
        try:
            for item in section_root.rglob('*'):
                if not item.is_file() or item.is_symlink():
                    continue
                rel = item.relative_to(section_root).as_posix()
                if is_ignored_statement_section_entry(rel):
                    continue
                if rel not in seed_defaults:
                    return False
        except OSError:
            return False
        for rel, default_text in seed_defaults.items():
            if _read_optional_text(section_root / rel, default_text) != default_text:
                return False
        return True

    def _statement_summary_status(workspace: Path) -> dict[str, object]:
        languages = statement_languages(workspace)
        if not languages:
            return {'text': 'none', 'danger': False, 'warn': True}
        if _statement_is_initial_empty(workspace, languages):
            return {'text': 'empty', 'danger': False, 'warn': False}
        if len(languages) <= 2:
            return {'text': ', '.join(languages), 'danger': False, 'warn': False}
        return {'text': f'{languages[0]} (+{len(languages) - 1})', 'danger': False, 'warn': False}

    nav: dict[str, dict[str, object]] = {}
    workspace_path_raw = _row_value(cast(dict[str, object], ctx['workspace']), 'path', '')
    workspace_path_text = cast(str | None, workspace_path_raw) or ''
    workspace_path = Path(workspace_path_text) if workspace_path_text else Path('.')
    general_cfg = cast(dict[str, object], ctx['general_cfg'])
    time_limit_ms = _to_int(general_cfg.get('time_limit_ms'), int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']))
    memory_limit_mb = _to_int(general_cfg.get('memory_limit_mb'), int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']))
    mode_text = normalize_problem_mode(general_cfg.get('mode'), str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
    pass_limit = normalize_pass_limit(general_cfg.get('pass_limit'), int(_C.GENERAL_CONFIG_DEFAULTS['pass_limit']))
    time_text = _compact_time_limit_label(time_limit_ms)
    memory_text = _compact_memory_limit_label(memory_limit_mb)
    general_parts = [time_text, memory_text]
    if pass_limit > 1:
        general_parts.append(f'{pass_limit} passes')
    general_parts.append(mode_text)
    nav['general'] = {'text': ', '.join(general_parts), 'danger': False}
    nav['statement_languages'] = _statement_summary_status(workspace_path)
    workspace_changes = cast(dict[str, object], ctx['workspace_changes'])
    changes_total = _to_int(workspace_changes.get('total'))
    nav['files'] = {'text': 'clean' if changes_total <= 0 else f'{changes_total} changed', 'danger': False}
    generator_status = cast(dict[str, object], ctx['generator_status'])
    generator_mode = cast(str | None, generator_status.get('mode')) or ''
    configured_rows = cast(list[dict[str, object]], generator_status.get('configured_sources') or [])
    configured_count = 0
    configured_ready = 0
    configured_paths: list[str] = []
    source_paths: list[str] = []
    for row in configured_rows:
        row_path = cast(str | None, row.get('path')) or ''
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
        workspace_path_raw = _row_value(cast(dict[str, object], ctx['workspace']), 'path', '')
        workspace_path_text = cast(str | None, workspace_path_raw) or ''
        if workspace_path_text:
            try:
                used_count = _count_used_configured_generators(Path(workspace_path_text), configured_paths, source_paths)
            except Exception:
                used_count = 0
        generator_text = f'{count_label(configured_count, "file")}, {used_count} used'
        generator_danger = configured_ready < configured_count
    else:
        generator_text = cast(str | None, generator_status.get('display')) or 'missing'
        generator_danger = generator_mode in {'missing', 'invalid'}
    nav['generators'] = {'text': generator_text, 'danger': bool(generator_danger)}
    checker_status = cast(dict[str, object], ctx['checker_status'])
    checker_display = cast(str | None, checker_status.get('display')) or 'unknown'
    checker_mode = cast(str | None, checker_status.get('mode')) or ''
    checker_applies = mode_text != 'interactive'
    checker_hint = ''
    checker_text = checker_display
    standard_checker = cast(str | None, checker_status.get('standard_checker')) or ''
    if checker_applies and standard_checker:
        std_name = standard_checker[5:] if standard_checker.startswith('std::') else standard_checker
        description = str(_C.STANDARD_CHECKER_DESCRIPTIONS.get(std_name, 'general-purpose standard checker from testlib'))
        checker_hint = f'Matches standard checker: {standard_checker} - {description}'
        checker_text = standard_checker
    nav['checker'] = {
        'text': checker_text if checker_applies else 'uses interactor',
        'danger': checker_applies and (checker_mode in {'missing', 'none'} or checker_display in {'unknown', 'error', 'missing'}),
        'hint': checker_hint,
    }
    interactor_status = cast(dict[str, object], ctx['interactor_status'])
    interactor_mode = cast(str | None, interactor_status.get('mode')) or ''
    interactor_display = cast(str | None, interactor_status.get('display')) or 'missing'
    nav['interactor'] = {'text': interactor_display, 'danger': interactor_mode in {'missing', 'none', 'invalid'}}
    validator_status = cast(dict[str, object], ctx['validator_status'])
    validator_mode = cast(str | None, validator_status.get('mode')) or ''
    validator_display = cast(str | None, validator_status.get('display')) or 'missing'
    nav['validator'] = {'text': validator_display, 'danger': validator_mode in {'missing', 'none', 'invalid'}}
    tests_status = cast(dict[str, object], ctx['tests_spec_status'])
    tests_mode = cast(str | None, tests_status.get('mode')) or ''
    tests_total = _to_int(tests_status.get('total'))
    tests_sample = _to_int(tests_status.get('sample'))
    tests_display = cast(str | None, tests_status.get('display')) or 'empty'
    tests_text = f'{tests_total} ({count_label(tests_sample, "sample")})' if tests_total > 0 else tests_display
    nav['tests'] = {'text': tests_text, 'danger': tests_mode in {'empty', 'invalid', 'missing', 'none'}, 'has_counts': tests_total > 0, 'total': tests_total, 'sample': tests_sample, 'sample_zero': tests_total > 0 and tests_sample <= 0}
    solutions_status = cast(dict[str, object], ctx['solutions_status'])
    solutions_mode = cast(str | None, solutions_status.get('mode')) or ''
    if solutions_mode == 'missing-main':
        count_display = cast(str | None, solutions_status.get('count_display')) or ''
        solutions_text = f'{count_display} (no main correct)' if count_display else 'no main correct'
    else:
        solutions_count_display = cast(str | None, solutions_status.get('count_display'))
        solutions_display = cast(str | None, solutions_status.get('display'))
        solutions_text = solutions_count_display or solutions_display or 'missing'
    solutions_danger = solutions_mode != 'ready'
    nav['solutions'] = {'text': solutions_text, 'danger': solutions_danger}
    verification_status = cast(dict[str, object], ctx['verification_status'])
    verification_mode_raw = cast(str | None, verification_status.get('mode'))
    verification_display_raw = cast(str | None, verification_status.get('display'))
    verification_mode = verification_mode_raw or verification_display_raw or 'none'
    verification_display = verification_display_raw or 'none'
    nav['run'] = {'text': verification_display, 'danger': verification_mode in {'none', 'failed'}, 'warn': verification_mode == 'stale'}
    workspace_row = cast(dict[str, object], ctx['workspace'])
    problem_row = cast(dict[str, object], ctx['problem'])
    workspace_id = _to_int(_row_value(workspace_row, 'id', 0))
    problem_id = _to_int(_row_value(problem_row, 'id', 0))
    workspace_path_raw = _row_value(workspace_row, 'path', '')
    workspace_head_raw = _row_value(workspace_row, 'head_commit', '')
    workspace_path_text = cast(str | None, workspace_path_raw) or ''
    workspace_head = cast(str | None, workspace_head_raw) or ''
    workspace_revision = cast(int | None, ctx.get('workspace_version'))
    head_revision = workspace_revision if workspace_revision is not None and workspace_revision > 0 else None
    if head_revision is None and workspace_path_text and workspace_head:
        head_revision = git_commit_count(Path(workspace_path_text), workspace_head)
    export_revision: int | None = None
    if workspace_id > 0 and problem_id > 0 and workspace_path_text:
        export_source_commit = config.export_service.latest_workspace_source_commit(problem_id, workspace_id)
        if export_source_commit:
            export_revision = git_commit_count(Path(workspace_path_text), export_source_commit)
    if export_revision is not None and export_revision > 0:
        export_outdated = head_revision is not None and head_revision > 0 and (export_revision != head_revision)
        nav['export'] = {'text': f'built for v{export_revision}', 'danger': bool(export_outdated)}
    else:
        nav['export'] = {'text': 'missing', 'danger': True}
    access_role = cast(str | None, cast(dict[str, object], ctx['access']).get('role')) or 'none'
    nav['access'] = {'text': access_role, 'danger': False}
    nav['workspace'] = nav['access']
    return nav

def render_workspace_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)], *, show_access_admin: bool=False):
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
        return template_response(request, 'access.html', {'ctx': ctx, 'message': message, 'acl_entries': acl_entries, 'repo_role_options': ['write', 'read']})

    workspace_changes = cast(dict[str, object], ctx['workspace_changes'])
    change_rows = cast(list[dict[str, object]], workspace_changes.get('rows') or [])
    requested_path = normalize_workspace_rel_path(request.query_params.get('path'))
    selected_path = ''
    if requested_path and any((row.get('link_path') == requested_path for row in change_rows)):
        selected_path = requested_path

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
