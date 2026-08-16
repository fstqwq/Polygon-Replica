from pathlib import Path

from fastapi import HTTPException, Request

from app.impl.auth.session import session_identity
from app.impl.runtime.dependency import runtime
from app.service.agent.service import (
    AgentGeneralPermissionRequired,
    AgentPermissionRequired,
    AgentProblemIdentity,
    AgentSessionIdentity,
)
from app.service.auth.model import AuthSessionIdentity
from app.service.repository.workspace import WorkspaceContext


AGENT_SESSION_HEADER = "X-Polygon-Agent-Session-ID"
AGENT_IDENTITY_HEADER = "X-Polygon-Agent-Identity-Hash"


def current_web_user(request: Request) -> AuthSessionIdentity:
    identity = session_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="login required")
    return identity


def _identity_headers(request: Request) -> tuple[str, str]:
    session_values = request.headers.getlist(AGENT_SESSION_HEADER)
    identity_values = request.headers.getlist(AGENT_IDENTITY_HEADER)
    if len(session_values) != 1 or len(identity_values) != 1:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_identity_required"},
        )
    session_id = session_values[0]
    identity_hash = identity_values[0]
    if not session_id or not identity_hash:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_identity_required"},
        )
    return session_id, identity_hash


def require_agent_session(request: Request) -> AgentSessionIdentity:
    session_id, identity_hash = _identity_headers(request)
    try:
        return runtime().agent_service.session_identity(
            agent_session_id=session_id,
            identity_hash=identity_hash,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_identity_invalid"},
        ) from exc


def require_agent_general(
    request: Request,
    *,
    min_scope: str,
) -> AgentSessionIdentity:
    session_id, identity_hash = _identity_headers(request)
    try:
        return runtime().agent_service.require_general_scope(
            agent_session_id=session_id,
            identity_hash=identity_hash,
            minimum_scope=min_scope,
        )
    except AgentGeneralPermissionRequired as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "agent_general_permission_required",
                "required_scope": exc.required_scope,
                "settings_url": f"{str(request.base_url).rstrip('/')}/agent/sessions",
            },
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_identity_invalid"},
        ) from exc


def explicit_problem_query(request: Request) -> str:
    values = request.query_params.getlist("problem")
    if len(values) != 1 or not values[0]:
        raise HTTPException(
            status_code=400,
            detail={"error": "exactly_one_problem_required"},
        )
    return values[0]


def require_agent_problem(
    request: Request,
    *,
    min_scope: str,
) -> AgentProblemIdentity:
    session_id, identity_hash = _identity_headers(request)
    problem = explicit_problem_query(request)
    try:
        return runtime().agent_service.problem_identity(
            agent_session_id=session_id,
            identity_hash=identity_hash,
            problem=problem,
            minimum_scope=min_scope,
        )
    except AgentPermissionRequired as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "agent_permission_required",
                "problem": exc.problem,
                "required_scope": exc.required_scope,
            },
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "problem_not_found"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_problem"},
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_identity_invalid"},
        ) from exc


def workspace_context_for_identity(
    identity: AgentProblemIdentity,
) -> WorkspaceContext:
    ctx = runtime().workspace_service.workspace_context(
        identity.problem_slug,
        identity.username,
        include_recent=False,
    )
    if int(ctx["problem"]["id"]) != identity.problem_id:
        raise HTTPException(status_code=403, detail="problem context mismatch")
    if int(ctx["user"]["id"]) != identity.user_id:
        raise HTTPException(status_code=403, detail="user context mismatch")
    workspace_row = ctx["workspace"]
    workspace = Path(str(workspace_row["path"])).resolve()
    try:
        with runtime().workspace_service.workspace_lock(workspace):
            status = runtime().workspace_service.read_workspace_status(workspace)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    workspace_row["branch"] = str(status.get("branch") or "main")
    workspace_row["head_commit"] = str(status.get("head_commit") or "")
    workspace_row["dirty"] = 1 if bool(status.get("dirty")) else 0
    return ctx


def workspace_path_for_identity(identity: AgentProblemIdentity) -> Path:
    ctx = workspace_context_for_identity(identity)
    return Path(str(ctx["workspace"]["path"])).resolve()
