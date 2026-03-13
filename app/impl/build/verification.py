from __future__ import annotations

from app.impl.preview.shared import (
    Form,
    Path,
    allocate_verification_id,
    allocate_run_id,
    audit,
    redirect_response,
    require_write_access,
    run_solution_options_context,
    start_verification_job,
    workspace_rel_file_exists,
    normalize_verification_target_page,
    config,
    normalize_expected_behavior,
    page_ctx,
)
def verification_start(problem: str, user: str, page: str=Form('statement')):
    target_page = normalize_verification_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    workspace_head = str(ctx['workspace'].get('head_commit') or '').strip()
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    verification_id = allocate_verification_id()
    backend_name = config.judgehost_task_service.backend_name()
    verification_details: dict[str, object] = {'status': 'running', 'steps': ['gen', 'val', 'run', 'check'], 'workspace_head': workspace_head, 'workspace_dirty': workspace_dirty, 'verification_id': verification_id, 'verification_backend': backend_name, 'error': ''}
    msg = 'verification running'
    try:
        solution_options, accepted_source, _ = run_solution_options_context(workspace)
        accepted_source = str(accepted_source or '').strip()
        if not accepted_source:
            raise ValueError('main correct solution is required')
        if not workspace_rel_file_exists(workspace, accepted_source):
            raise ValueError('main correct solution source does not exist')
        targets: list[dict[str, str]] = []
        for row in solution_options:
            source_path = str(row.get('path') or '').strip()
            if not source_path:
                continue
            expected_behavior = normalize_expected_behavior(str(row.get('expected_behavior') or 'unknown'))
            if source_path == accepted_source:
                expected_behavior = 'accepted'
            if expected_behavior == 'unknown' and bool(row.get('is_accepted')):
                expected_behavior = 'accepted'
            targets.append({'path': source_path, 'expected_behavior': expected_behavior})
        if not targets:
            raise ValueError('at least one solution source is required')
        if not any((str(item.get('expected_behavior') or '') == 'accepted' for item in targets)):
            raise ValueError('accepted solution source is required')
        targets.sort(key=lambda item: (0 if item.get('expected_behavior') == 'accepted' else 1, str(item.get('path') or '')))
        planned_run_ids: list[str] = []
        for target in targets:
            run_token = allocate_run_id()
            target['run_id'] = run_token
            planned_run_ids.append(run_token)
        verification_details['submission_paths'] = [str(item.get('path') or '') for item in targets]
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
    if str(verification_details.get('status') or '') == 'failed':
        audit(ctx['user']['id'], ctx['problem']['id'], 'verification.start', verification_details)
    return redirect_response(base, status_code=303, message=msg)


