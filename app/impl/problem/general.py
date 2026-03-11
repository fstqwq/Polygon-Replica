from __future__ import annotations

import json
from pathlib import Path

from fastapi import Form, HTTPException

from app.impl.auth.public import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.public import (
    audit,
    coerce_int,
    normalize_problem_mode,
    normalize_problem_name_required,
    read_problem_config,
    require_write_access,
    page_ctx,
)

_C = config.constants


def general_save(problem: str, user: str, time_limit_ms: str=Form('2000'), memory_limit_mb: str=Form('1024'), mode: str=Form('pass-fail'), problem_name: str=Form('')):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        safe_time_limit = coerce_int(time_limit_ms, int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS)
        safe_memory = coerce_int(memory_limit_mb, int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB)
        safe_mode = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        requested_problem_name = str(problem_name or '').strip()
        current_problem_name = str(ctx['problem'].get('name') or '').strip()
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


