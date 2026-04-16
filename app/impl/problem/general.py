from __future__ import annotations
from app.impl.auth.session import require_session_user

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import Form, HTTPException, Depends

from app.impl.auth.shared import redirect_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.service.statement.context import normalize_statement_language, pick_statement_language, statement_languages
from app.service.verification.runtime import coerce_int, normalize_pass_limit, normalize_problem_mode
from app.impl.workspace.problem_config import read_problem_config

_C = config.constants


def general_save(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    time_limit_ms: Annotated[str, Form()] = '2000',
    memory_limit_mb: Annotated[str, Form()] = '1024',
    mode: Annotated[str, Form()] = 'pass-fail',
    pass_limit: Annotated[str, Form()] = '1',
    language: Annotated[str, Form()] = '',
    preview_id: Annotated[str, Form()] = '',
):
    ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        safe_time_limit = coerce_int(time_limit_ms, int(_C.GENERAL_CONFIG_DEFAULTS['time_limit_ms']), _C.GENERAL_TIME_LIMIT_MIN_MS, _C.GENERAL_TIME_LIMIT_MAX_MS)
        safe_memory = coerce_int(memory_limit_mb, int(_C.GENERAL_CONFIG_DEFAULTS['memory_limit_mb']), _C.GENERAL_MEMORY_LIMIT_MIN_MB, _C.GENERAL_MEMORY_LIMIT_MAX_MB)
        safe_mode = normalize_problem_mode(mode, str(_C.GENERAL_CONFIG_DEFAULTS['mode']))
        safe_pass_limit = normalize_pass_limit(
            pass_limit,
            int(_C.GENERAL_CONFIG_DEFAULTS['pass_limit']),
            min_value=_C.GENERAL_PASS_LIMIT_MIN,
            max_value=_C.GENERAL_PASS_LIMIT_MAX,
        )
        with config.workspace_service.workspace_lock(workspace):
            payload, _, cfg_path = read_problem_config(workspace)
            payload.pop('interactive', None)
            payload.update({'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode, 'pass_limit': safe_pass_limit})
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        audit(ctx['user']['id'], ctx['problem']['id'], 'general.save', {'time_limit_ms': safe_time_limit, 'memory_limit_mb': safe_memory, 'mode': safe_mode, 'pass_limit': safe_pass_limit})
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    query: dict[str, str] = {}
    safe_language = normalize_statement_language(language)
    available_languages = statement_languages(workspace)
    if safe_language and (
        safe_language in available_languages
        or ((not available_languages) and safe_language == pick_statement_language(workspace))
    ):
        query['language'] = safe_language
    safe_preview_id = str(preview_id or '').strip()
    if safe_preview_id:
        query['preview_id'] = safe_preview_id
    redirect_url = f'/problems/{problem}/statement'
    if query:
        redirect_url = f'{redirect_url}?{urlencode(query)}'
    return redirect_response(redirect_url, status_code=303, message=msg)



