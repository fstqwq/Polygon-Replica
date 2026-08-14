from app.service.access.model import (
    AccessDecision,
    AccessRole,
    Actor,
    AgentScope,
    Capability,
    Resource,
)

_ROLE_LEVEL = {"none": 0, "read": 1, "write": 2, "owner": 3, "admin": 4}
_AGENT_SCOPE_LEVEL = {"readonly": 1, "workspace": 2, "commit": 3}
_CAPABILITY_LEVEL: dict[Capability, int] = {
    "problem.read": 1,
    "problem.write": 2,
    "problem.manage": 3,
    "workspace.read": 1,
    "workspace.write": 2,
    "workspace.manage": 3,
    "verification.view": 1,
    "verification.rejudge": 1,
    "verification.cancel": 5,
    "package.list": 1,
    "package.download": 1,
    "package.create": 2,
    "contest.read": 1,
    "contest.write": 2,
    "contest.manage": 3,
    "contest.roster": 3,
    "contest.build": 2,
    "contest.package": 1,
}


def repo_role(raw_role: str) -> AccessRole:
    if raw_role == "read":
        return "read"
    if raw_role == "write":
        return "write"
    if raw_role == "owner":
        return "owner"
    raise ValueError("invalid repo role")


def contest_role(raw_role: str) -> AccessRole:
    return repo_role(raw_role)


def transferable_repo_role(raw_role: str) -> AccessRole:
    role = repo_role(raw_role.strip().lower())
    if role == "owner":
        raise ValueError("owner access is fixed and cannot be transferred")
    return role


def transferable_contest_role(raw_role: str) -> AccessRole:
    role = contest_role(raw_role.strip().lower())
    if role == "owner":
        raise ValueError("owner access is fixed and cannot be transferred")
    return role


def access_role(raw_role: str | None) -> AccessRole:
    if raw_role is None:
        return "none"
    if raw_role == "none":
        return "none"
    if raw_role == "read":
        return "read"
    if raw_role == "write":
        return "write"
    if raw_role == "owner":
        return "owner"
    if raw_role == "admin":
        return "admin"
    raise RuntimeError("invalid access role")


def stronger_role(left: AccessRole, right: AccessRole) -> AccessRole:
    return left if _ROLE_LEVEL[left] >= _ROLE_LEVEL[right] else right


def derived_problem_role(member_role: AccessRole) -> AccessRole:
    if member_role in {"owner", "write"}:
        return "write"
    if member_role == "read":
        return "read"
    raise ValueError("contest membership cannot derive problem access")


def role_decision(
    actor: Actor,
    resource: Resource,
    capability: Capability,
    role: AccessRole,
) -> AccessDecision:
    required = _CAPABILITY_LEVEL[capability]
    allowed = required <= _ROLE_LEVEL[role]
    if capability == "verification.cancel":
        allowed = actor.workspace_id is not None and actor.workspace_id == resource.workspace_id
    reason = "" if allowed else _denial_reason(capability, role, resource)
    return AccessDecision(actor, resource, capability, role, allowed, reason)


def _denial_reason(
    capability: Capability,
    role: AccessRole,
    _resource: Resource,
) -> str:
    if capability == "verification.cancel":
        return "You are not the owner of this verification"
    problem_read_capabilities = {
        "problem.read",
        "verification.view",
        "verification.rejudge",
        "package.list",
        "package.download",
    }
    if capability in problem_read_capabilities:
        return "you do not have access to this problem"
    if capability in {"problem.write", "package.create"}:
        return "read-only access" if role == "read" else "write access required"
    if capability == "problem.manage":
        return "owner or admin access required"
    if capability in {"workspace.read", "workspace.write", "workspace.manage"}:
        return "workspace belongs to another user"
    if capability == "contest.read":
        return "you do not have access to this contest"
    if capability in {"contest.write", "contest.build"}:
        return "read-only access" if role == "read" else "write access required"
    if capability in {"contest.manage", "contest.roster"}:
        return "contest owner or system admin access required"
    if capability == "contest.package":
        return "you do not have access to this contest"
    return "access denied"


def agent_scope(raw_scope: str) -> AgentScope:
    if raw_scope == "readonly":
        return "readonly"
    if raw_scope == "workspace":
        return "workspace"
    if raw_scope == "commit":
        return "commit"
    raise ValueError("invalid scope")


def agent_scope_allows(granted_scope: str, minimum_scope: str) -> bool:
    granted = agent_scope(granted_scope)
    minimum = agent_scope(minimum_scope)
    return _AGENT_SCOPE_LEVEL[granted] >= _AGENT_SCOPE_LEVEL[minimum]


def effective_agent_scope(token_scope: str, role: AccessRole) -> AgentScope | None:
    token = agent_scope(token_scope)
    role_level = {
        "none": 0,
        "read": 1,
        "write": 3,
        "owner": 3,
        "admin": 3,
    }[role]
    level = min(_AGENT_SCOPE_LEVEL[token], role_level)
    if level >= _AGENT_SCOPE_LEVEL["commit"]:
        return "commit"
    if level >= _AGENT_SCOPE_LEVEL["workspace"]:
        return "workspace"
    if level >= _AGENT_SCOPE_LEVEL["readonly"]:
        return "readonly"
    return None


def stronger_agent_scope(left: str, right: str) -> AgentScope:
    left_scope = agent_scope(left)
    right_scope = agent_scope(right)
    if _AGENT_SCOPE_LEVEL[left_scope] >= _AGENT_SCOPE_LEVEL[right_scope]:
        return left_scope
    return right_scope
