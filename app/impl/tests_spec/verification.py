from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated

from fastapi import Form, Depends

from app.impl.auth.shared import redirect_response
from app.impl.runtime.dependency import runtime
from app.impl.tests_spec.shared import normalize_verification_target_page
from app.impl.workspace.access import require_read_access
from app.impl.workspace.context_job import start_verification_job
from app.impl.workspace.context_ui import page_ctx
from app.impl.workspace.context_job_helper import allocate_verification_id
from app.impl.workspace.context_operation import run_solution_options_context, workspace_rel_file_exists
from app.service.problem.solution_metadata import ExpectedBehavior, normalize_expected_behavior
from app.service.verification.types import VerificationTarget


def verification_start(problem: str, user: Annotated[str, Depends(require_session_user)], page: str=Form('statement')):
    target_page = normalize_verification_target_page(page)
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=True, include_recent=False)
    require_read_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    workspace_head = ctx['workspace']['head_commit']
    workspace_dirty = bool(ctx['workspace'].get('dirty'))
    verification_id = allocate_verification_id()
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
        sources: list[tuple[str, ExpectedBehavior]] = []
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
            sources.append((source_path, expected_behavior))
        if not sources:
            raise ValueError('at least one solution source is required')
        if not any(expected == 'accepted' for _, expected in sources):
            raise ValueError('accepted solution source is required')
        sources.sort(key=lambda item: (0 if item[1] == 'accepted' else 1, item[0]))
        targets: list[VerificationTarget] = []
        solution_index = 0
        for source_path, expected_behavior in sources:
            if source_path == accepted_source:
                program_id = 'accepted'
            else:
                program_id = f'solution-{solution_index}'
                solution_index += 1
            targets.append({
                'path': source_path,
                'expected_behavior': expected_behavior,
                'program_id': program_id,
            })
        started = start_verification_job(
            runtime(),
            problem,
            user,
            problem_id=int(ctx['problem']['id']),
            workspace_id=int(ctx['workspace']['id']),
            workspace_head=workspace_head,
            workspace_dirty=workspace_dirty,
            targets=targets,
            verification_id=verification_id,
            allow_package_certification=bool(ctx['access']['can_create_packages']),
            workspace_path=workspace,
        )
        msg = 'verification running' if started else 'verification already running'
    except Exception as exc:
        msg = f'verification failed: {exc}'
    base = f'/problems/{problem}/{target_page}'
    return redirect_response(base, status_code=303, message=msg)
