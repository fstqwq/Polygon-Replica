from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import Form, HTTPException

from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit, normalize_problem_name_required
from app.impl.workspace.problem_config import coerce_int, normalize_problem_mode, read_problem_config
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_job import page_ctx

_C = config.constants


def general_save(
    problem: str,
    user: str,
    time_limit_ms: Annotated[str, Form()] = '2000',
    memory_limit_mb: Annotated[str, Form()] = '1024',
    mode: Annotated[str, Form()] = 'pass-fail',
    problem_name: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        safe_time_limit = coerce_int(time_limit_ms, int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS)
        safe_memory = coerce_int(memory_limit_mb, int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB)
        safe_mode = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        requested_problem_name = problem_name.strip()
        current_problem_name = ctx['problem']['name'].strip()
        safe_problem_name = normalize_problem_name_required(requested_problem_name or current_problem_name)
        with config.workspace_service.workspace_lock(workspace):
            payload, _, cfg_path = read_problem_config(workspace)
            payload.pop('interactive', None)
            payload.update({'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode})
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            config.workspace_service.set_problem_name(problem, safe_problem_name)
        audit(ctx['user']['id'], ctx['problem']['id'], 'general.save', {'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode, 'problem_name': safe_problem_name})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    return redirect_response(f'/problems/{problem}/{user}/statement', status_code=303, message=msg)


