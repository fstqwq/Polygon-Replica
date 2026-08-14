import app.main_constant as _K

from pathlib import Path
from typing import Callable, Literal, TypedDict
from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app.impl.auth.session import has_sudo_session
from app.impl.auth.shared import redirect_response, safe_next_path
from app.impl.runtime.dependency import runtime
from app.impl.workspace.access import require_write_access
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_operation import (
    read_build_config,
    write_build_config,
)
from app.impl.workspace.context_ui import page_ctx
from app.main_util import normalize_component_source_path
from app.service.platform.workspace_path import (
    normalize_workspace_rel_path,
    safe_workspace_path,
)

MAIN_CORRECT_EXPECTED_VALUE = "main_correct"
MAIN_CORRECT_EXPECTED_LABEL = "main correct solution (AC)"


class SingleSourceEditorContext(TypedDict):
    state: Literal["empty", "create", "existing"]
    source_path: str
    source_exists: bool
    content: str
    content_truncated: bool
    starter_content: str


def _normalize_component_create_path(raw: str | None, folder: str, default_filename: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    expected_prefix = f"{folder}/"
    if normalized and (not normalized.startswith(expected_prefix)):
        normalized = f"{folder}/{normalized}"
    return normalize_component_source_path(normalized, folder, default_filename)


def single_source_editor_context(
    *,
    request: Request,
    workspace: Path,
    configured_source: str,
    configured_source_exists: bool,
    folder: str,
    default_filename: str,
    starter_content: str,
) -> SingleSourceEditorContext:
    default_source = f"{folder}/{default_filename}"
    requested_new = request.query_params.get("new")
    source_path = configured_source or default_source
    state: Literal["empty", "create", "existing"]
    if configured_source_exists:
        state = "existing"
    elif requested_new is None:
        state = "empty"
    else:
        state = "create"
        try:
            source_path = _normalize_component_create_path(
                requested_new or default_filename,
                folder,
                default_filename,
            )
        except ValueError:
            source_path = default_source

    content = ""
    content_truncated = False
    if state == "existing":
        try:
            source_abs = safe_workspace_path(workspace, source_path)
            if source_abs.exists() and source_abs.is_file() and not source_abs.is_symlink():
                content, content_truncated = runtime().git_service.read_file_limited(
                    workspace,
                    source_path,
                    runtime().config_values.integer(
                        "WORKSPACE_FILE_VIEW_CHAR_LIMIT"
                    ),
                )
        except HTTPException:
            content = ""
            content_truncated = False

    return {
        "state": state,
        "source_path": source_path,
        "source_exists": configured_source_exists,
        "content": content,
        "content_truncated": content_truncated,
        "starter_content": "" if configured_source_exists else starter_content,
    }


def _normalize_component_rename_target(raw: str | None, folder: str, default_filename: str, component_label: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        raise ValueError(f"new {component_label} source is required")
    expected_prefix = f"{folder}/"
    if not normalized.startswith(expected_prefix):
        normalized = f"{folder}/{normalized}"
    return normalize_component_source_path(normalized, folder, default_filename)


def _normalize_component_rename_source(raw: str | None, folder: str, default_filename: str, component_label: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    if not normalized:
        raise ValueError(f"{component_label} source is required")
    return normalize_component_source_path(normalized, folder, default_filename)


def rename_component_source(
    *,
    problem: str,
    user: str,
    old_path: str,
    new_path: str,
    folder: str,
    default_filename: str,
    component_label: str,
    redirect_url_for_path: Callable[[str], str],
    config_key: str = "",
    ctx: dict | None = None,
) -> RedirectResponse:
    source_for_redirect = f"{folder}/{default_filename}"
    active_ctx = ctx
    if active_ctx is None:
        active_ctx = page_ctx(problem, user, include_branches=False, refresh_status=False, include_recent=False)
    require_write_access(active_ctx)
    workspace = Path(active_ctx["workspace"]["path"])
    msg = f"{component_label} source renamed"
    try:
        old_source = _normalize_component_rename_source(old_path, folder, default_filename, component_label)
        new_source = _normalize_component_rename_target(new_path, folder, default_filename, component_label)
        source_for_redirect = old_source
        if old_source == new_source:
            msg = f"{component_label} source rename skipped"
        else:
            with runtime().workspace_service.workspace_lock(workspace):
                old_abs = safe_workspace_path(workspace, old_source)
                if old_abs.is_symlink() or (not old_abs.exists()) or (not old_abs.is_file()):
                    raise ValueError(f"{component_label} source does not exist")
                new_abs = safe_workspace_path(workspace, new_source)
                if new_abs.exists():
                    raise ValueError("destination source already exists")
                if new_abs.parent.exists() and (not new_abs.parent.is_dir()):
                    raise ValueError("destination parent is not a directory")
                build_cfg, cfg_path = read_build_config(workspace)
                config_changed = False
                if config_key == "generator_sources" and old_source in (
                    sources := build_cfg["generator_sources"]
                ):
                    build_cfg["generator_sources"] = [
                        new_source if source == old_source else source
                        for source in sources
                    ]
                    config_changed = True
                elif config_key == "accepted_solution_source":
                    build_cfg["accepted_solution_source"] = new_source
                    config_changed = True
                elif config_key == "checker_source":
                    build_cfg["checker_source"] = new_source
                    config_changed = True
                elif config_key == "interactor_source":
                    build_cfg["interactor_source"] = new_source
                    config_changed = True
                elif config_key == "validator_source":
                    build_cfg["validator_source"] = new_source
                    config_changed = True
                runtime().git_service.rename_path(
                    workspace,
                    old_source,
                    new_source,
                )
                try:
                    if config_changed:
                        write_build_config(cfg_path, build_cfg)
                except (OSError, ValueError):
                    runtime().git_service.rename_path(
                        workspace,
                        new_source,
                        old_source,
                    )
                    raise
            source_for_redirect = new_source
    except (ValueError, OSError) as exc:
        msg = str(exc)
    except HTTPException as exc:
        msg = str(exc.detail)
    return redirect_response(redirect_url_for_path(source_for_redirect), status_code=303, message=msg)


def _settings_user_ctx(user: str) -> dict:
    gctx = global_user_ctx(user)
    if not isinstance(user_row_raw := gctx.get("user"), dict):
        raise HTTPException(status_code=400, detail="invalid user")
    if not isinstance(user_id := user_row_raw.get("id"), int) or not isinstance(username := user_row_raw.get("username"), str):
        raise HTTPException(status_code=400, detail="invalid user")
    is_system_admin = user_row_raw.get("is_system_admin")
    if not isinstance(is_system_admin, int) or isinstance(is_system_admin, bool):
        raise HTTPException(status_code=400, detail="invalid user")
    user_row = {
        "id": user_id,
        "username": username,
        "is_system_admin": is_system_admin,
    }
    if user_id <= 0 or not username:
        raise HTTPException(status_code=400, detail="invalid user")
    default_problem = gctx.get("default_problem")
    if default_problem is None:
        default_problem = ""
    elif not isinstance(default_problem, str):
        raise HTTPException(status_code=400, detail="invalid default problem")
    return {"user": user_row, "default_problem": default_problem}


def _sudo_redirect_for_destructive(
    next_path: str,
    message: str = "sudo proof required for destructive operation",
) -> RedirectResponse:
    safe_next = safe_next_path(next_path, "/")
    return redirect_response(f"/sudo?next={quote_plus(safe_next)}", status_code=303, message=message)


def _has_destructive_sudo_for_ctx(request: Request, ctx: dict) -> bool:
    user_row = ctx.get("user") if isinstance(ctx, dict) else None
    user_id = 0
    if isinstance(user_row, dict):
        try:
            user_id = int(user_row["id"])
        except Exception:
            user_id = 0
    if user_id <= 0:
        return False
    return has_sudo_session(request, user_id=user_id, scope=str(_K.SUDO_SCOPE_DESTRUCTIVE))
