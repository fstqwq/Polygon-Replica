import app.main_constant as _K

import logging
from pathlib import Path
from typing import Annotated, TypedDict, cast

from fastapi import Depends, HTTPException, Request

from app.impl.auth.session import has_sudo_session, require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.workspace_scope import (
    ContestWorkspaceContext,
    contest_workspace_context_from_request,
)
from app.impl.runtime.dependency import runtime

from app.main_util import normalize_workspace_rel_path
from app.service.statement.constant import (
    STATEMENT_SECTIONS_DIR,
    is_ignored_statement_section_entry,
)
from app.service.statement.context import statement_languages
from app.service.statement.render import default_statement_title_for_workspace

from app.impl.workspace.access import (
    problem_acl_entries,
    require_read_access,
    workspace_access_context,
)
from app.impl.workspace.artifact import artifact_version_number
from app.impl.workspace.context import count_label
from app.impl.workspace.context_operation import (
    _solutions_status_context,
    _tests_spec_status_context,
)
from app.impl.workspace.context_component_status import (
    checker_status_context,
    generator_status_context,
    interactor_status_context,
    validator_status_context,
)
from app.service.platform.git_process import run_git
from app.service.problem.authoring_source import inspect_authoring_source
from app.service.problem.content_review import problem_content_review
from app.service.problem.readiness import WorkspaceReadinessSubject
from app.service.problem.runtime_config import problem_config_limits
from app.service.repository.revision import workspace_revision_info
from app.service.problem.resource_limits import resource_limit_display

logger = logging.getLogger(__name__)


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
            {'label': 'Program input/output limit', 'value': f'{int(runtime().config_values.RUN_EXEC_OUTPUT_KB)} KiB'},
            {'label': 'Compilation size limit', 'value': f'{int(runtime().config_values.TOOLCHAIN_COMPILE_OUTPUT_KB)} KiB'},
            {'label': 'Saved judging log limit', 'value': f'{int(runtime().config_values.JUDGEHOST_STORED_LOG_LIMIT_BYTES)} bytes'},
        ],
    }


def _published_build_text(workspace: Path, head_commit: str) -> str | None:
    if not head_commit:
        return None
    result = run_git(
        [
            "git",
            "-C",
            str(workspace),
            "show",
            f"{head_commit}:config/build.json",
        ]
    )
    return result.stdout if result.returncode == 0 else None


def page_ctx(
    problem: str,
    user: str,
    include_branches: bool = True,
    refresh_status: bool = True,
    include_recent: bool = True,
    include_workspace_changes: bool = True,
    contest_workspace: ContestWorkspaceContext | None = None,
) -> dict:
    _ = include_branches
    try:
        problem_id, user_id = runtime().workspace_service.page_identity(problem, user)
        access = workspace_access_context(problem_id, user_id)
        require_read_access({'access': access})
        if refresh_status:
            # Provision without the lock-side refresh; the explicit refresh below updates DB once.
            runtime().workspace_service.ensure_workspace(problem, user, refresh_status=False)
        ctx = runtime().workspace_service.workspace_context(problem, user, include_recent=include_recent)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    ctx['access'] = access
    ctx['workspace_access'] = runtime().access_query.workspace_context(
        problem_id=problem_id,
        actor_user_id=user_id,
        workspace_id=int(ctx['workspace']['id']),
    )
    ctx['branches'] = ['main']
    ctx['branches_truncated'] = False
    ctx['branch_limit'] = 1
    workspace_path = Path(ctx['workspace']['path'])
    auto_updated = False
    if refresh_status:
        try:
            auto_updated = runtime().workspace_merge_service.advance_clean_workspace(workspace_path)
        except Exception:
            logger.exception("clean workspace auto-update failed for %s", problem)
    ctx['workspace_auto_update_message'] = (
        'Workspace updated to the published revision.' if auto_updated else ''
    )
    undo_context = runtime().workspace_merge_service.undo_context(workspace_path)
    ctx['workspace_merge_result'] = undo_context or {}
    ctx['workspace_has_merge_undo'] = undo_context is not None
    if refresh_status:
        live_status: dict[str, object] | None = None
        try:
            live_status = cast(
                dict[str, object],
                runtime().workspace_service.refresh_workspace_status_with_ids(
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
    limits = runtime().config_values.snapshot()
    with runtime().workspace_service.workspace_lock(workspace_path):
        source_state = inspect_authoring_source(
            workspace_path,
            problem_limits=problem_config_limits(runtime().config_values),
            tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                limits["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
            allow_repair=bool(ctx['workspace_access']['can_write']),
            published_build_text=_published_build_text(
                workspace_path,
                workspace_head,
            ),
        )
    if source_state['build_normalized']:
        workspace_dirty = True
        ctx['workspace']['dirty'] = 1
        try:
            normalized_status = runtime().workspace_service.refresh_workspace_status_with_ids(
                workspace_path,
                int(ctx['problem']['id']),
                int(ctx['user']['id']),
            )
            ctx['workspace']['head_commit'] = (
                cast(str | None, normalized_status.get('head_commit')) or ''
            )
            ctx['workspace']['dirty'] = (
                1 if bool(normalized_status.get('dirty')) else 0
            )
        except Exception:
            logger.exception(
                "workspace status refresh after source normalization failed for %s",
                problem,
            )
    ctx['authoring_source'] = source_state
    general_cfg = source_state['problem']
    build_cfg = source_state['build']
    safe_mode = general_cfg['mode']
    ctx['problem_mode'] = safe_mode
    time_limit_ms = general_cfg['time_limit_ms']
    memory_limit_mb = general_cfg['memory_limit_mb']
    ctx['general_cfg'] = {
        'time_limit_ms': time_limit_ms,
        'memory_limit_mb': memory_limit_mb,
        'mode': safe_mode,
        'pass_limit': general_cfg['pass_limit'],
        **resource_limit_display(time_limit_ms, memory_limit_mb),
    }
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
        ctx['checker_status'] = checker_status_context(workspace_path, build_cfg)
    except Exception:
        ctx['checker_status'] = {'mode': 'missing', 'display': 'unknown', 'standard_checker': '', 'standard_expected_checker': '', 'standard_warning': '', 'standard_valid': False, 'repo_source': 'checkers/checker.cpp', 'repo_source_exists': False}
    try:
        ctx['generator_status'] = generator_status_context(
            workspace_path,
            build_cfg,
        )
    except Exception:
        ctx['generator_status'] = {
            'mode': 'missing',
            'display': 'missing',
            'repo_source': 'generators/generator.cpp',
            'repo_source_exists': False,
            'source_rows': [],
            'source_rows_truncated': False,
        }
    try:
        ctx['interactor_status'] = interactor_status_context(workspace_path, build_cfg)
    except Exception:
        ctx['interactor_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'interactors/interactor.cpp', 'repo_source_exists': False}
    try:
        ctx['validator_status'] = validator_status_context(workspace_path, build_cfg)
    except Exception:
        ctx['validator_status'] = {'mode': 'missing', 'display': 'missing', 'repo_source': 'validators/validator.cpp', 'repo_source_exists': False}
    try:
        ctx['solutions_status'] = _solutions_status_context(workspace_path, build_cfg)
    except Exception:
        ctx['solutions_status'] = {'mode': 'missing', 'display': 'missing', 'accepted_source': '', 'accepted_exists': False, 'count': 0, 'count_display': '0 files', 'truncated': False}
    try:
        ctx['tests_spec_status'] = _tests_spec_status_context(workspace_path)
    except Exception:
        ctx['tests_spec_status'] = {'mode': 'invalid', 'display': 'invalid', 'total': 0, 'manual': 0, 'gen': 0, 'sample': 0}
    statement_language_names = statement_languages(workspace_path)
    if safe_mode == 'interactive':
        output_component_label = 'Interactor'
        output_component_status = ctx['interactor_status']
        output_component_display = str(output_component_status['display'])
    else:
        output_component_label = 'Checker'
        output_component_status = ctx['checker_status']
        output_component_display = str(
            output_component_status['standard_checker']
            or output_component_status['display']
        )
    ctx['content_review'] = problem_content_review(
        time_limit_ms=time_limit_ms,
        memory_limit_mb=memory_limit_mb,
        test_count=int(ctx['tests_spec_status']['total']),
        tests_valid=ctx['tests_spec_status']['mode'] != 'invalid',
        solution_count=int(ctx['solutions_status']['count']),
        solutions_truncated=bool(ctx['solutions_status']['truncated']),
        main_solution_ready=ctx['solutions_status']['mode'] == 'ready',
        output_component_label=output_component_label,
        output_component_display=output_component_display,
        output_component_ready=output_component_status['mode'] == 'repository',
        validator_display=str(ctx['validator_status']['display']),
        validator_ready=ctx['validator_status']['mode'] == 'repository',
        statement_language_names=statement_language_names,
        source_issues=source_state['issues'],
    )
    empty_changes = {'counts': {'added': 0, 'modified': 0, 'deleted': 0, 'renamed': 0, 'untracked': 0, 'conflicted': 0, 'typechange': 0, 'other': 0}, 'rows': [], 'total': 0, 'truncated': False, 'limit': None}
    if include_workspace_changes:
        try:
            ctx['workspace_changes'] = runtime().git_service.status_change_summary(workspace_path)
        except Exception:
            ctx['workspace_changes'] = empty_changes
    else:
        ctx['workspace_changes'] = empty_changes
    readiness_subject: WorkspaceReadinessSubject = {
        'problem_id': int(ctx['problem']['id']),
        'workspace_id': int(ctx['workspace']['id']),
        'workspace_path': workspace_path,
        'head_commit': workspace_head,
        'dirty': workspace_dirty,
        'local_revision': ctx['workspace_revision']['local'],
        'upstream_revision': ctx['workspace_revision']['upstream'],
        'needs_update': bool(ctx['workspace_needs_update']),
    }
    try:
        ctx['readiness'] = runtime().problem_readiness_service.readiness(
            readiness_subject,
            explain_verification=True,
        )
    except Exception:
        logger.exception("problem readiness projection failed for %s", problem)
        ctx['readiness'] = runtime().problem_readiness_service.unavailable(
            readiness_subject
        )
    latest_verification = ctx.get('latest_artifact_verification')
    ctx['latest_verification_version'] = artifact_version_number(latest_verification['id']) if latest_verification else None
    ctx['nav_status'] = _build_problem_nav_status(ctx)
    ctx['contest_workspace'] = contest_workspace
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
    time_limit_ms = int(general_cfg['time_limit_ms'])
    memory_limit_mb = int(general_cfg['memory_limit_mb'])
    mode_text = str(general_cfg['mode'])
    pass_limit = int(general_cfg['pass_limit'])
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
    source_rows = cast(list[dict[str, object]], generator_status.get('source_rows') or [])
    if source_rows:
        used_count = sum(1 for row in source_rows if _to_int(row.get('reference_count')) > 0)
        generator_text = f'{count_label(len(source_rows), "file")}, {used_count} used'
        generator_danger = False
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
        description = str(_K.STANDARD_CHECKER_DESCRIPTIONS.get(std_name, 'general-purpose standard checker from testlib'))
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
    readiness = cast(dict[str, object], ctx['readiness'])
    verification_status = cast(dict[str, object], readiness['verification'])
    verification_tone = cast(str, verification_status['tone'])
    nav['run'] = {
        'text': cast(str, verification_status['display']),
        'danger': verification_tone == 'danger',
        'warn': verification_tone == 'warning',
    }
    problem_row = cast(dict[str, object], ctx['problem'])
    problem_id = _to_int(_row_value(problem_row, 'id', 0))
    package_status = cast(dict[str, object], readiness['package'])
    package_state = cast(str, package_status['state'])
    package_revision = cast(int | None, package_status['revision_number'])
    if package_state == 'ready' and package_revision is not None:
        package_text = f'v{package_revision}'
    elif package_state == 'stale' and package_revision is not None:
        package_text = f'v{package_revision} (stale)'
    else:
        package_text = 'none'
    export_nav: dict[str, object] = {
        'text': package_text,
        'danger': package_state == 'none',
        'warn': package_state == 'stale',
    }
    export_source_commit = runtime().export_service.latest_source_commit(problem_id)
    if export_source_commit and problem_id > 0:
        current_export = runtime().export_service.latest_succeeded_export_job(
            problem_id,
            export_source_commit,
            'icpc',
        )
        if (
            current_export is not None
            and current_export['export_id']
            and current_export['filename']
        ):
            export_id = current_export['export_id']
            export_filename = Path(current_export['filename']).name
            if runtime().export_service.export_archive_path(
                problem_id,
                export_id,
                export_filename,
            ) is not None:
                export_nav['download'] = {
                    'export_id': export_id,
                    'filename': export_filename,
                }
    nav['export'] = export_nav
    access_role = cast(str | None, cast(dict[str, object], ctx['access']).get('role')) or 'none'
    nav['access'] = {'text': access_role, 'danger': False}
    nav['workspace'] = nav['access']
    return nav

def render_workspace_page(request: Request, problem: str, user: Annotated[str, Depends(require_session_user)], *, show_access_admin: bool=False):
    ctx = page_ctx(
        problem,
        user,
        contest_workspace=contest_workspace_context_from_request(request),
    )
    workspace = Path(ctx['workspace']['path'])
    message = ''
    has_destructive_sudo = has_sudo_session(
        request,
        user_id=int(ctx['user']['id']),
        scope=str(_K.SUDO_SCOPE_DESTRUCTIVE),
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
            selected_diff, selected_diff_truncated = runtime().git_service.diff_for_path(workspace, selected_path)
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
    return template_response(request, 'workspace.html', {'ctx': ctx, 'branches': ctx.get('branches', []), 'message': message, 'selected_path': selected_path, 'selected_diff': selected_diff, 'selected_diff_truncated': bool(selected_diff_truncated), 'selected_diff_lines': selected_diff_lines, 'change_rows': change_rows, 'has_destructive_sudo': bool(has_destructive_sudo)})
