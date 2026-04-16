from __future__ import annotations

from pathlib import Path
from typing import cast

from fastapi import HTTPException, Request

from app.impl.auth.session import session_identity
from app.impl.runtime.config import config


def current_web_user(request: Request) -> dict[str, object]:
    identity = session_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="login required")
    return dict(identity)


def bearer_token(request: Request) -> str:
    raw = str(request.headers.get("authorization") or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def require_agent_token(request: Request, *, min_scope: str):
    token = bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return config.agent_service.require_token(token, min_scope=min_scope)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def workspace_context_for_identity(identity) -> dict[str, object]:
    ctx = config.workspace_service.workspace_context(identity.problem_slug, identity.username, include_recent=False)
    if int(ctx["problem"]["id"]) != int(identity.problem_id):
        raise HTTPException(status_code=403, detail="problem context mismatch")
    if int(ctx["user"]["id"]) != int(identity.user_id):
        raise HTTPException(status_code=403, detail="user context mismatch")
    workspace_row = cast(dict[str, object], ctx["workspace"])
    workspace = Path(str(workspace_row["path"])).resolve()
    try:
        with config.workspace_service.workspace_lock(workspace):
            status = config.workspace_service.read_workspace_status(workspace)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    workspace_row["branch"] = str(status.get("branch") or "main")
    workspace_row["head_commit"] = str(status.get("head_commit") or "")
    workspace_row["dirty"] = 1 if bool(status.get("dirty")) else 0
    return ctx


def workspace_path_for_identity(identity) -> Path:
    ctx = workspace_context_for_identity(identity)
    return Path(str(ctx["workspace"]["path"])).resolve()
