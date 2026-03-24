from __future__ import annotations

from pathlib import Path

from fastapi import Form

from app.impl.auth.shared import redirect_response
from app.impl.tests_spec.shared import normalize_verification_target_page
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_job import start_verification_job
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_job_helper import allocate_run_id, allocate_verification_id
from app.impl.workspace.context_operation import audit, run_solution_options_context, workspace_rel_file_exists
from app.service.problem.solution_metadata import normalize_expected_behavior


def _empty_task_counts() -> dict[str, object]:
    return {
        "total": 0,
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "by_kind": {
            "generate-input": {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
            "main-correct": {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
            "solution-run": {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0},
        },
    }


def verification_start(problem: str, user: str, page: str=Form('statement')):
    target_page = normalize_verification_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    workspace_head = ctx['workspace']['head_commit']
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    verification_id = allocate_verification_id()
    verification_details: dict[str, object] = {'status': 'running', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'verification_id': verification_id, 'error': ''}
    msg = 'verification running'
    try:
        solution_options, accepted_source, _ = run_solution_options_context(workspace)
        if not isinstance(accepted_source, str):
            accepted_source = ''
        else:
            accepted_source = accepted_source.strip()
        if not accepted_source:
            raise ValueError('main correct solution is required')
        if not workspace_rel_file_exists(workspace, accepted_source):
            raise ValueError('main correct solution source does not exist')
        targets: list[dict[str, str]] = []
        for row in solution_options:
            if not isinstance(source_path := row.get('path'), str):
                continue
            source_path = source_path.strip()
            if not source_path:
                continue
            expected_behavior = normalize_expected_behavior(expected_behavior if isinstance(expected_behavior := row.get('expected_behavior'), str) else 'unknown')
            if source_path == accepted_source:
                expected_behavior = 'accepted'
            if expected_behavior == 'unknown' and bool(row.get('is_accepted')):
                expected_behavior = 'accepted'
            targets.append({'path': source_path, 'expected_behavior': expected_behavior})
        if not targets:
            raise ValueError('at least one solution source is required')
        if not any(item['expected_behavior'] == 'accepted' for item in targets):
            raise ValueError('accepted solution source is required')
        targets.sort(key=lambda item: (0 if item['expected_behavior'] == 'accepted' else 1, item['path']))
        planned_run_ids: list[str] = []
        for target in targets:
            run_token = allocate_run_id()
            target['run_id'] = run_token
            planned_run_ids.append(run_token)
        verification_details['submission_paths'] = [item['path'] for item in targets]
        verification_details['solution_count'] = len(targets)
        started = start_verification_job(
            problem,
            user,
            actor_user_id=int(ctx['user']['id']),
            problem_id=int(ctx['problem']['id']),
            workspace_id=int(ctx['workspace']['id']),
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=targets,
            verification_id=verification_id,
            initial_details=verification_details,
            workspace_path=workspace,
        )
        msg = 'verification running' if started else 'verification already running'
    except Exception as exc:
        verification_details['status'] = 'failed'
        verification_details['error'] = str(exc)
        msg = f'verification failed: {exc}'
    base = f'/problems/{problem}/{user}/{target_page}'
    if verification_details['status'] == 'failed':
        audit(ctx['user']['id'], ctx['problem']['id'], 'verification.start', verification_details)
    return redirect_response(base, status_code=303, message=msg)



