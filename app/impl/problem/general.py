from app.impl.auth.session import require_session_user

from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlencode

from fastapi import Form, HTTPException, Depends

from app.impl.auth.shared import redirect_response
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import (
    workspace_rel_file_exists,
    write_build_config,
)
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context_ui import page_ctx
from app.service.statement.context import normalize_statement_language, statement_languages
from app.service.problem.build_config import (
    BUILD_CONFIG_REL,
    inspect_authoring_build_config,
)
from app.service.problem.runtime_config import (
    PROBLEM_CONFIG_REL,
    ProblemConfig,
    ProblemMode,
    dumps_problem_config,
    problem_config_limits,
)
from app.service.platform.workspace_path import safe_workspace_path


_BUILD_SOURCE_KEYS = (
    'accepted_solution_source',
    'validator_source',
    'checker_source',
    'interactor_source',
)


def _cleanup_build_config_for_mode(
    workspace: Path,
    mode: ProblemMode,
) -> None:
    try:
        build_cfg_path = safe_workspace_path(
            workspace,
            BUILD_CONFIG_REL.as_posix(),
        )
        inspected = inspect_authoring_build_config(
            build_cfg_path.read_text(encoding="utf-8"),
            problem_mode=mode,
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return
    if inspected["error"]:
        return
    build_cfg = inspected["config"]
    original = dict(build_cfg)

    for key in _BUILD_SOURCE_KEYS:
        source = build_cfg.get(key)
        if source and (not workspace_rel_file_exists(workspace, str(source))):
            build_cfg.pop(key, None)

    if (
        build_cfg != original
        or inspected["extra_fields"]
        or inspected["removed_keys"]
    ):
        write_build_config(build_cfg_path, build_cfg)


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
    ctx = page_ctx(
        problem,
        user,
        include_branches=False,
        refresh_status=False,
        include_recent=False,
    )
    require_write_access(ctx)
    workspace = Path(ctx['workspace']['path'])
    msg = 'saved'
    try:
        limits = problem_config_limits(runtime().config_values)
        try:
            safe_time_limit = int(time_limit_ms)
            safe_memory = int(memory_limit_mb)
            safe_pass_limit = int(pass_limit)
        except ValueError as exc:
            raise ValueError("problem limits must be integers") from exc
        if not limits.min_time_limit_ms <= safe_time_limit <= limits.max_time_limit_ms:
            raise ValueError(
                "time limit must be between "
                f"{limits.min_time_limit_ms} and {limits.max_time_limit_ms} ms"
            )
        if not limits.min_memory_limit_mb <= safe_memory <= limits.max_memory_limit_mb:
            raise ValueError(
                "memory limit must be between "
                f"{limits.min_memory_limit_mb} and {limits.max_memory_limit_mb} MiB"
            )
        if not limits.min_pass_limit <= safe_pass_limit <= limits.max_pass_limit:
            raise ValueError(
                "pass limit must be between "
                f"{limits.min_pass_limit} and {limits.max_pass_limit}"
            )
        if mode not in {"pass-fail", "interactive"}:
            raise ValueError("problem mode must be pass-fail or interactive")
        safe_mode = cast(ProblemMode, mode)
        with runtime().workspace_service.workspace_lock(workspace):
            cfg_path = safe_workspace_path(
                workspace,
                PROBLEM_CONFIG_REL.as_posix(),
            )
            payload = ProblemConfig(
                time_limit_ms=safe_time_limit,
                memory_limit_mb=safe_memory,
                mode=safe_mode,
                pass_limit=safe_pass_limit,
            )
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                dumps_problem_config(payload, limits=limits),
                encoding='utf-8',
                newline='\n',
            )
            _cleanup_build_config_for_mode(workspace, safe_mode)
    except (ValueError, OSError, HTTPException) as exc:
        msg = str(exc)
    query: dict[str, str] = {}
    safe_language = normalize_statement_language(language)
    available_languages = statement_languages(workspace)
    if safe_language and (safe_language in available_languages):
        query['language'] = safe_language
    safe_preview_id = str(preview_id or '').strip()
    if safe_preview_id:
        query['preview_id'] = safe_preview_id
    redirect_url = f'/problems/{problem}/statement'
    if query:
        redirect_url = f'{redirect_url}?{urlencode(query)}'
    return redirect_response(redirect_url, status_code=303, message=msg)
