from dataclasses import dataclass
from typing import Literal, TypedDict


AccessRole = Literal["none", "read", "write", "owner", "admin"]
DirectProblemRole = Literal["none", "read", "write", "owner"]
AgentScope = Literal["readonly", "workspace", "commit"]
AgentGeneralScope = Literal["none", "readonly", "workspace", "commit"]
ResourceKind = Literal[
    "problem",
    "workspace",
    "contest",
    "verification",
    "package",
]
Capability = Literal[
    "problem.read",
    "problem.write",
    "problem.access.manage",
    "problem.manage",
    "workspace.read",
    "workspace.write",
    "workspace.manage",
    "verification.view",
    "verification.rejudge",
    "verification.cancel",
    "package.list",
    "package.download",
    "package.create",
    "contest.read",
    "contest.write",
    "contest.manage",
    "contest.roster",
    "contest.build",
    "contest.package",
]


@dataclass(frozen=True)
class Actor:
    user_id: int
    workspace_id: int | None = None
    agent_scope: AgentScope | None = None


@dataclass(frozen=True)
class Resource:
    kind: ResourceKind
    resource_id: str
    problem_id: int | None = None
    workspace_id: int | None = None
    actor_user_id: int | None = None
    status: str = ""


@dataclass(frozen=True)
class AccessDecision:
    actor: Actor
    resource: Resource
    capability: Capability
    role: AccessRole
    allowed: bool
    reason: str


@dataclass(frozen=True)
class ProblemAccessChange:
    problem_id: int
    target_user_id: int
    original_role: DirectProblemRole
    requested_role: DirectProblemRole


class AccessMutationResult(TypedDict):
    target_user_id: int
    target_username: str
    previous_role: str
    role: str


class ProblemAccessContext(TypedDict):
    role: AccessRole
    can_read: bool
    can_write: bool
    can_manage_access: bool
    can_manage: bool
    can_view_verification: bool
    can_rejudge: bool
    can_list_packages: bool
    can_download_packages: bool
    can_create_packages: bool
    read_block_reason: str
    write_block_reason: str
    access_manage_block_reason: str
    manage_block_reason: str
    verification_block_reason: str
    rejudge_block_reason: str
    package_list_block_reason: str
    package_download_block_reason: str
    package_create_block_reason: str


class WorkspaceAccessContext(TypedDict):
    can_read: bool
    can_write: bool
    can_manage: bool
    read_block_reason: str
    write_block_reason: str
    manage_block_reason: str


class ContestAccessContext(TypedDict):
    role: AccessRole
    can_read: bool
    can_write: bool
    can_manage: bool
    can_manage_roster: bool
    can_build: bool
    can_download_packages: bool
    read_block_reason: str
    write_block_reason: str
    manage_block_reason: str
    roster_block_reason: str
    build_block_reason: str
    package_block_reason: str


class VerificationAccessContext(TypedDict):
    can_view: bool
    can_rejudge: bool
    can_cancel: bool
    owns_verification: bool
    view_block_reason: str
    rejudge_block_reason: str
    cancel_block_reason: str


class PackageJobAccessContext(TypedDict):
    can_view: bool
    can_download: bool
    view_block_reason: str
    download_block_reason: str


class ProblemParticipationRow(TypedDict):
    slug: str
    role: AccessRole
    workspace_id: int | None
    path: str
    branch: str
    head_commit: str
    dirty: int
    revision_local: int | None
    revision_upstream: int | None
    revision_missing: int
    revision_highlight: int
    revision_upstream_higher: int
    revision_ahead_count: int | None
    revision_behind_count: int | None
    updated_at: str
    last_updated_at: str


class ProblemAclEntry(TypedDict):
    user_id: int
    username: str
    role: str
    created_at: str
    is_system_admin: int
