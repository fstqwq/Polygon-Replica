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


def current_web_user(request: Request) -> AuthSessionIdentity:
    identity = session_identity(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="login required")
    return identity


def _bearer_credential(request: Request) -> str:
    values = request.headers.getlist("Authorization")
    if len(values) != 1:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_credential_required"},
        )
    scheme, separator, credential = values[0].partition(" ")
    if scheme.lower() != "bearer" or separator != " " or not credential:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_credential_required"},
        )
    return credential


def require_agent_session(request: Request) -> AgentSessionIdentity:
    credential = _bearer_credential(request)
    try:
        return runtime().agent_service.session_identity(
            credential=credential,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "agent_credential_invalid"},
        ) from exc


def require_agent_general(
    request: Request,
    *,
    min_scope: str,
) -> AgentSessionIdentity:
    credential = _bearer_credential(request)
    try:
        return runtime().agent_service.require_general_scope(
            credential=credential,
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
            detail={"error": "agent_credential_invalid"},
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
    credential = _bearer_credential(request)
    problem = explicit_problem_query(request)
    try:
        return runtime().agent_service.problem_identity(
            credential=credential,
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
            detail={"error": "agent_credential_invalid"},
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
