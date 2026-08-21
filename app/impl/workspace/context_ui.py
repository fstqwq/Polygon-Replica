import app.main_constant as _K

import logging
from pathlib import Path
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from app.impl.auth.session import has_sudo_session, require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.workspace_scope import (
    ContestWorkspaceContext,
    contest_workspace_context_from_request,
)
from app.impl.runtime.dependency import runtime

from app.service.platform.workspace_path import normalize_workspace_rel_path
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
from app.impl.workspace.context_model import (
    CheckerComponentContext,
    GeneratorComponentContext,
    PackageDownloadContext,
    ProblemComponentsContext,
    ProblemPageContext,
    ProblemShellContext,
    SolutionsComponentContext,
    SourceComponentContext,
    SystemLimitInfo,
    navigation_context,
    workspace_published_revision_pair,
)
from app.impl.workspace.context_operation import (
    _solutions_status_context,
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
from app.service.problem.context import (
    ProblemTestsContext,
    StatusContext,
    metadata_context,
    status_context,
    tests_status_context,
)
from app.service.problem.readiness import PackageReadiness, WorkspaceReadinessSubject
from app.service.problem.runtime_config import problem_config_limits
from app.service.repository.git import StatusChangeSummary
from app.service.repository.revision import workspace_revision_info

logger = logging.getLogger(__name__)


def _system_limit_info() -> SystemLimitInfo:
    upload_limit_bytes = runtime().config_values.integer("UPLOAD_MAX_BYTES")
    return {
        'title': 'System limits',
        'description': 'Contact an administrator to change these limits if needed.',
        'rows': [
            {
                'label': 'Program input/output and upload limit',
                'value': f'{upload_limit_bytes} bytes',
            },
            {'label': 'Compilation size limit', 'value': f'{runtime().config_values.integer("TOOLCHAIN_COMPILE_OUTPUT_KB")} KiB'},
            {'label': 'Saved judging log limit', 'value': f'{runtime().config_values.integer("JUDGEHOST_STORED_LOG_LIMIT_BYTES")} bytes'},
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


def _statement_seed_defaults(workspace: Path) -> dict[str, str]:
    return {
        "name.tex": default_statement_title_for_workspace(workspace) + "\n",
        "legend.tex": "",
        "input.tex": "",
        "output.tex": "",
        "interaction.tex": "",
        "notes.tex": "",
    }


def _read_optional_text(path: Path, fallback: str) -> str:
    try:
        if path.exists() and path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8")
    except OSError:
        return fallback
    return fallback


def _statement_is_initial_empty(workspace: Path, languages: list[str]) -> bool:
    if not languages:
        return True
    if len(languages) != 1:
        return False
    section_root = workspace / STATEMENT_SECTIONS_DIR / languages[0]
    if (
        not section_root.exists()
        or not section_root.is_dir()
        or section_root.is_symlink()
    ):
        return True
    seed_defaults = _statement_seed_defaults(workspace)
    try:
        for item in section_root.rglob("*"):
            if not item.is_file() or item.is_symlink():
                continue
            relative = item.relative_to(section_root).as_posix()
            if is_ignored_statement_section_entry(relative):
                continue
            if relative not in seed_defaults:
                return False
    except OSError:
        return False
    return all(
        _read_optional_text(section_root / relative, default_text) == default_text
        for relative, default_text in seed_defaults.items()
    )


def _statement_status(workspace: Path, languages: list[str]) -> StatusContext:
    if not languages:
        return status_context(state="missing", text="none", tone="warning")
    if _statement_is_initial_empty(workspace, languages):
        return status_context(state="empty", text="empty")
    if len(languages) <= 2:
        return status_context(state="ready", text=", ".join(languages))
    return status_context(
        state="ready",
        text=f"{languages[0]} (+{len(languages) - 1})",
    )


def _current_domjudge_download(
    problem_id: int,
    package: PackageReadiness,
) -> PackageDownloadContext | None:
    if package["state"] != "ready" or not package["published_commit"]:
        return None
    current_export = runtime().export_service.latest_succeeded_export_job(
        problem_id,
        package["published_commit"],
        "domjudge",
    )
    if (
        current_export is None
        or not current_export["export_id"]
        or not current_export["filename"]
    ):
        return None
    export_id = current_export["export_id"]
    filename = Path(current_export["filename"]).name
    return {
        "export_id": export_id,
        "filename": filename,
    }


def page_ctx(
    problem: str,
    user: str,
    include_branches: bool = True,
    refresh_status: bool = True,
    include_recent: bool = True,
    include_workspace_changes: bool = True,
    contest_workspace: ContestWorkspaceContext | None = None,
) -> ProblemPageContext:
    try:
        problem_id, user_id = runtime().workspace_service.page_identity(problem, user)
        access = workspace_access_context(problem_id, user_id)
        require_read_access({'access': access})
        if refresh_status:
            # Provision without the lock-side refresh; the explicit refresh below updates DB once.
            runtime().workspace_service.ensure_workspace(problem, user, refresh_status=False)
        base_ctx = runtime().workspace_service.workspace_context(
            problem,
            user,
            include_recent=include_recent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    ctx = cast(ProblemPageContext, dict(base_ctx))
    ctx['access'] = access
    ctx['workspace_access'] = runtime().access_query.workspace_context(
        problem_id=problem_id,
        actor_user_id=user_id,
        workspace_id=int(ctx['workspace']['id']),
    )
    ctx['branches'] = ['main'] if include_branches else []
    ctx['branches_truncated'] = False
    ctx['branch_limit'] = 1 if include_branches else 0
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
    with runtime().workspace_service.workspace_lock(workspace_path):
        source_state = inspect_authoring_source(
            workspace_path,
            problem_limits=problem_config_limits(runtime().config_values),
            tests_spec_max_bytes=runtime().config_values.integer(
                "TEXTAREA_MAX_BYTES"
            ),
            statement_sample_max_bytes=runtime().config_values.integer(
                "STATEMENT_SAMPLE_MAX_BYTES"
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
    general_cfg = source_state['problem']
    build_cfg = source_state['build']
    safe_mode = general_cfg['mode']
    metadata = metadata_context(general_cfg)
    time_limit_ms = metadata['time_limit_ms']
    memory_limit_mb = metadata['memory_limit_mb']
    ctx['system_limit_info'] = _system_limit_info()
    workspace_revision = workspace_revision_info(
        workspace_path,
        workspace_branch,
        workspace_head=workspace_head,
        workspace_dirty=workspace_dirty,
    )
    behind_count = workspace_revision['behind_count'] or 0
    workspace_needs_update = bool(
        workspace_revision['upstream_higher'] or behind_count > 0
    )
    if safe_mode == 'interactive':
        checker_status: CheckerComponentContext = {
            'mode': 'not-applicable',
            'display': 'not used',
            'standard_checker': '',
            'standard_expected_checker': '',
            'standard_warning': '',
            'standard_valid': True,
            'repo_source': '',
            'repo_source_exists': False,
        }
    else:
        try:
            checker_status = checker_status_context(workspace_path, build_cfg)
        except Exception:
            checker_status = {'mode': 'missing', 'display': 'unknown', 'standard_checker': '', 'standard_expected_checker': '', 'standard_warning': '', 'standard_valid': False, 'repo_source': 'checkers/checker.cpp', 'repo_source_exists': False}
    try:
        generator_status: GeneratorComponentContext = generator_status_context(
            workspace_path,
            source_state['tests'],
            build_cfg,
        )
    except Exception:
        generator_status = {
            'mode': 'missing',
            'display': 'missing',
            'repo_source': 'generators/generator.cpp',
            'repo_source_exists': False,
            'source_rows': [],
            'configured_sources': [],
            'source_rows_truncated': False,
        }
    if safe_mode == 'interactive':
        try:
            interactor_status: SourceComponentContext = interactor_status_context(
                workspace_path,
                build_cfg,
            )
        except Exception:
            interactor_status = {'mode': 'missing', 'display': 'missing', 'repo_source': 'interactors/interactor.cpp', 'repo_source_exists': False}
    else:
        interactor_status = {
            'mode': 'not-applicable',
            'display': 'not used',
            'repo_source': '',
            'repo_source_exists': False,
        }
    try:
        validator_status: SourceComponentContext = validator_status_context(
            workspace_path,
            build_cfg,
        )
    except Exception:
        validator_status = {'mode': 'missing', 'display': 'missing', 'repo_source': 'validators/validator.cpp', 'repo_source_exists': False}
    try:
        solutions_status: SolutionsComponentContext = _solutions_status_context(
            workspace_path,
            build_cfg,
        )
    except Exception:
        solutions_status = {
            'mode': 'missing',
            'display': 'missing',
            'accepted_source': '',
            'accepted_exists': False,
            'count': 0,
            'count_display': '0 files',
            'truncated': False,
            'entries': [],
        }
    tests_status: ProblemTestsContext = tests_status_context(
        source_state['tests'],
        valid=source_state['tests_valid'],
    )
    statement_language_names = statement_languages(workspace_path)
    components: ProblemComponentsContext = {
        'checker': checker_status,
        'interactor': interactor_status,
        'validator': validator_status,
        'generators': generator_status,
        'solutions': solutions_status,
        'tests': tests_status,
        'statements': _statement_status(workspace_path, statement_language_names),
    }
    if safe_mode == 'interactive':
        output_component_label = 'Interactor'
        output_component_status = interactor_status
        output_component_display = str(output_component_status['display'])
    else:
        output_component_label = 'Checker'
        output_component_status = checker_status
        output_component_display = str(
            output_component_status['standard_checker']
            or output_component_status['display']
        )
    content_review = problem_content_review(
        time_limit_ms=time_limit_ms,
        memory_limit_mb=memory_limit_mb,
        test_count=int(tests_status['total']),
        tests_valid=tests_status['mode'] != 'invalid',
        solution_count=int(solutions_status['count']),
        solutions_truncated=bool(solutions_status['truncated']),
        main_solution_ready=solutions_status['mode'] == 'ready',
        output_component_label=output_component_label,
        output_component_display=output_component_display,
        output_component_ready=output_component_status['mode'] == 'repository',
        validator_display=str(validator_status['display']),
        validator_ready=validator_status['mode'] == 'repository',
        statement_language_names=statement_language_names,
        source_issues=source_state['issues'],
    )
    empty_changes: StatusChangeSummary = {
        'counts': {
            'added': 0,
            'modified': 0,
            'deleted': 0,
            'renamed': 0,
            'untracked': 0,
            'conflicted': 0,
            'typechange': 0,
            'other': 0,
        },
        'rows': [],
        'total': 0,
        'truncated': False,
        'limit': None,
    }
    if include_workspace_changes:
        try:
            workspace_changes = runtime().git_service.status_change_summary(workspace_path)
        except Exception:
            workspace_changes = empty_changes
    else:
        workspace_changes = empty_changes
    readiness_subject: WorkspaceReadinessSubject = {
        'problem_id': int(ctx['problem']['id']),
        'workspace_id': int(ctx['workspace']['id']),
        'workspace_path': workspace_path,
        'head_commit': workspace_head,
        'dirty': workspace_dirty,
        'local_revision': workspace_revision['local'],
        'upstream_revision': workspace_revision['upstream'],
        'needs_update': workspace_needs_update,
    }
    try:
        readiness = runtime().problem_readiness_service.readiness(
            readiness_subject,
            explain_verification=True,
        )
    except Exception:
        logger.exception("problem readiness projection failed for %s", problem)
        readiness = runtime().problem_readiness_service.unavailable(
            readiness_subject
        )
    package_download = _current_domjudge_download(
        int(ctx['problem']['id']),
        readiness['package'],
    )
    access_role = str(access['role'])
    shell: ProblemShellContext = {
        'metadata': metadata,
        'components': components,
        'readiness': readiness,
        'content_review': content_review,
        'navigation': navigation_context(
            metadata=metadata,
            components=components,
            readiness=readiness,
            workspace_changes=workspace_changes,
            access_role=access_role,
            package_download=package_download,
        ),
        'workspace_changes': workspace_changes,
        'workspace_revision_pair': workspace_published_revision_pair(
            readiness['workspace']['local_revision'],
            readiness['workspace']['upstream_revision'],
            dirty=readiness['workspace']['dirty'],
            needs_update=readiness['workspace']['needs_update'],
        ),
    }
    ctx['shell'] = shell
    ctx['contest_workspace'] = contest_workspace
    ctx['page_wide_content'] = False
    return ctx

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

    shell = cast(ProblemShellContext, ctx['shell'])
    workspace_changes = shell['workspace_changes']
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
