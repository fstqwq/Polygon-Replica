from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from app.impl.auth.session import has_sudo_session
from app.impl.auth.shared import redirect_response, safe_next_path
from app.impl.runtime.config import config
from app.impl.workspace.context import global_user_ctx
from app.main_util import normalize_component_source_path, normalize_workspace_rel_path

_C = config.constants
_BINARY_SNIFF_BYTES = 8192

MAIN_CORRECT_EXPECTED_VALUE = "main_correct"
MAIN_CORRECT_EXPECTED_LABEL = "main correct solution (AC)"


def _looks_like_binary_file(path: Path, sniff_bytes: int = _BINARY_SNIFF_BYTES) -> bool:
    cap = max(1, int(sniff_bytes))
    try:
        with path.open("rb") as fh:
            chunk = fh.read(cap)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _normalize_component_create_path(raw: str | None, folder: str, default_filename: str) -> str:
    normalized = normalize_workspace_rel_path(raw)
    expected_prefix = f"{folder}/"
    if normalized and (not normalized.startswith(expected_prefix)):
        normalized = f"{folder}/{normalized}"
    return normalize_component_source_path(normalized, folder, default_filename)


def _settings_user_ctx(user: str) -> dict:
    gctx = global_user_ctx(user)
    if not isinstance(user_row_raw := gctx.get("user"), dict):
        raise HTTPException(status_code=400, detail="invalid user")
    if not isinstance(user_id := user_row_raw.get("id"), int) or not isinstance(username := user_row_raw.get("username"), str):
        raise HTTPException(status_code=400, detail="invalid user")
    user_row = {
        "id": user_id,
        "username": username,
    }
    if user_row["id"] <= 0 or (not user_row["username"]):
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
    return has_sudo_session(request, user_id=user_id, scope=str(_C.SUDO_SCOPE_DESTRUCTIVE))


def _as_bool_form_value(raw: str) -> bool:
    token = raw.strip().lower()
    return token in {"1", "true", "yes", "on", "y"}


def _system_config_row_by_key(sections: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    rows_by_key: dict[str, dict[str, object]] = {}
    for section in sections:
        rows = section.get("rows") if isinstance(section, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = key.strip() if isinstance(key := row.get("key"), str) else ""
            if key:
                rows_by_key[key] = row
    return rows_by_key




