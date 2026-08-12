from __future__ import annotations

from collections.abc import Mapping

from app.db import DB
from app.service.access.model import (
    AccessRole,
    Actor,
    ContestAccessContext,
    PackageJobAccessContext,
    ProblemParticipationRow,
    ProblemAccessContext,
    Resource,
    VerificationAccessContext,
    WorkspaceAccessContext,
)
from app.service.access.policy import (
    agent_scope,
    agent_scope_allows,
    effective_agent_scope,
    role_decision,
)
from app.service.access.store import AccessStore


class AccessQuery:
    def __init__(self, db: DB):
        self._store = AccessStore(db)

    def is_system_admin(self, user_id: int) -> bool:
        return self._store.is_system_admin(user_id)

    def problem_context(self, problem_id: int, user_id: int) -> ProblemAccessContext:
        return self.problem_contexts([problem_id], user_id)[int(problem_id)]

    def problem_contexts(
        self,
        problem_ids: list[int],
        user_id: int,
    ) -> dict[int, ProblemAccessContext]:
        ids = list(dict.fromkeys(int(problem_id) for problem_id in problem_ids))
        actor = Actor(int(user_id))
        roles = self._store.effective_problem_roles(ids, actor.user_id)
        return {
            problem_id: self._problem_context(
                actor,
                Resource("problem", str(problem_id), problem_id=problem_id),
                roles[problem_id],
            )
            for problem_id in ids
        }

    def direct_problem_context(self, problem_id: int, user_id: int) -> ProblemAccessContext:
        actor = Actor(int(user_id))
        role: AccessRole = (
            "admin"
            if self.is_system_admin(user_id)
            else self._store.direct_problem_role(problem_id, user_id)
        )
        return self._problem_context(
            actor,
            Resource("problem", str(problem_id), problem_id=int(problem_id)),
            role,
        )

    def workspace_context(
        self,
        *,
        problem_id: int,
        actor_user_id: int,
        workspace_id: int,
    ) -> WorkspaceAccessContext:
        problem = self.problem_context(problem_id, actor_user_id)
        owns_workspace = self._store.workspace_belongs_to_user(
            workspace_id,
            actor_user_id,
        )
        system_admin = self.is_system_admin(actor_user_id)
        can_read = problem["can_read"] and (owns_workspace or system_admin)
        can_write = problem["can_write"] and (owns_workspace or system_admin)
        can_manage = problem["can_read"] and (owns_workspace or system_admin)
        return {
            "can_read": can_read,
            "can_write": can_write,
            "can_manage": can_manage,
            "read_block_reason": "" if can_read else (
                problem["read_block_reason"]
                if owns_workspace
                else "workspace belongs to another user"
            ),
            "write_block_reason": "" if can_write else (
                problem["write_block_reason"]
                if owns_workspace
                else "workspace belongs to another user"
            ),
            "manage_block_reason": "" if can_manage else (
                problem["read_block_reason"]
                if owns_workspace
                else "workspace belongs to another user"
            ),
        }

    @staticmethod
    def _problem_context(
        actor: Actor,
        resource: Resource,
        role: AccessRole,
    ) -> ProblemAccessContext:
        read = role_decision(actor, resource, "problem.read", role)
        write = role_decision(actor, resource, "problem.write", role)
        manage = role_decision(actor, resource, "problem.manage", role)
        verification = role_decision(actor, resource, "verification.view", role)
        rejudge = role_decision(actor, resource, "verification.rejudge", role)
        package_list = role_decision(actor, resource, "package.list", role)
        package_download = role_decision(actor, resource, "package.download", role)
        package_create = role_decision(actor, resource, "package.create", role)
        return {
            "role": role,
            "can_read": read.allowed,
            "can_write": write.allowed,
            "can_manage": manage.allowed,
            "can_view_verification": verification.allowed,
            "can_rejudge": rejudge.allowed,
            "can_list_packages": package_list.allowed,
            "can_download_packages": package_download.allowed,
            "can_create_packages": package_create.allowed,
            "read_block_reason": read.reason,
            "write_block_reason": write.reason,
            "manage_block_reason": manage.reason,
            "verification_block_reason": verification.reason,
            "rejudge_block_reason": rejudge.reason,
            "package_list_block_reason": package_list.reason,
            "package_download_block_reason": package_download.reason,
            "package_create_block_reason": package_create.reason,
        }

    def contest_context(self, contest_id: int, user_id: int) -> ContestAccessContext:
        actor = Actor(int(user_id))
        resource = Resource("contest", str(contest_id))
        role = self._store.contest_role(contest_id, user_id)
        read = role_decision(actor, resource, "contest.read", role)
        write = role_decision(actor, resource, "contest.write", role)
        manage = role_decision(actor, resource, "contest.manage", role)
        roster = role_decision(actor, resource, "contest.roster", role)
        build = role_decision(actor, resource, "contest.build", role)
        package = role_decision(actor, resource, "contest.package", role)
        return {
            "role": role,
            "can_read": read.allowed,
            "can_write": write.allowed,
            "can_manage": manage.allowed,
            "can_manage_roster": roster.allowed,
            "can_build": build.allowed,
            "can_download_packages": package.allowed,
            "read_block_reason": read.reason,
            "write_block_reason": write.reason,
            "manage_block_reason": manage.reason,
            "roster_block_reason": roster.reason,
            "build_block_reason": build.reason,
            "package_block_reason": package.reason,
        }

    def manageable_problem_rows_excluding_contest(
        self,
        contest_id: int,
        user_id: int,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        return self._store.manageable_problem_rows_excluding_contest(
            contest_id,
            user_id,
            limit=limit,
        )

    def accessible_problem_slugs(self, user_id: int, *, limit: int) -> list[str]:
        if self.is_system_admin(user_id):
            return self._store.all_problem_slugs(limit=limit)
        return self._store.accessible_problem_slugs(user_id, limit=limit)

    def accessible_problem_slugs_by_leaf(
        self,
        user_id: int,
        leaf: str,
        *,
        limit: int,
    ) -> list[str]:
        if self.is_system_admin(user_id):
            return self._store.all_problem_slugs_by_leaf(leaf, limit=limit)
        return self._store.accessible_problem_slugs_by_leaf(
            user_id,
            leaf,
            limit=limit,
        )

    def participating_problem_rows(
        self,
        user_id: int,
        *,
        limit: int,
    ) -> list[ProblemParticipationRow]:
        return self._store.participating_problem_rows(user_id, limit=limit)

    def verification_context(
        self,
        *,
        actor_user_id: int,
        actor_workspace_id: int | None,
        expected_problem_id: int,
        verification: Mapping[str, object] | None,
        problem_access: ProblemAccessContext | None = None,
    ) -> VerificationAccessContext:
        if verification is None:
            return self._missing_verification_context()
        actor_owns_workspace = (
            actor_workspace_id is not None
            and self._store.workspace_belongs_to_user(
                actor_workspace_id,
                actor_user_id,
            )
        )
        actor = Actor(
            int(actor_user_id),
            workspace_id=actor_workspace_id if actor_owns_workspace else None,
        )
        return self._verification_context(
            actor=actor,
            expected_problem_id=expected_problem_id,
            verification=verification,
            problem_access=problem_access,
        )

    @staticmethod
    def _missing_verification_context() -> VerificationAccessContext:
        return {
            "can_view": False,
            "can_rejudge": False,
            "can_cancel": False,
            "owns_verification": False,
            "view_block_reason": "verification not found",
            "rejudge_block_reason": "verification not found",
            "cancel_block_reason": "verification not found",
        }

    def verification_contexts(
        self,
        *,
        actor_user_id: int,
        actor_workspace_id: int | None,
        expected_problem_id: int,
        verifications: list[Mapping[str, object]],
        problem_access: ProblemAccessContext | None = None,
    ) -> list[VerificationAccessContext]:
        actor_owns_workspace = (
            actor_workspace_id is not None
            and self._store.workspace_belongs_to_user(
                actor_workspace_id,
                actor_user_id,
            )
        )
        actor = Actor(
            int(actor_user_id),
            workspace_id=actor_workspace_id if actor_owns_workspace else None,
        )
        return [
            self._verification_context(
                actor=actor,
                expected_problem_id=expected_problem_id,
                verification=verification,
                problem_access=problem_access,
            )
            for verification in verifications
        ]

    def _verification_context(
        self,
        *,
        actor: Actor,
        expected_problem_id: int,
        verification: Mapping[str, object],
        problem_access: ProblemAccessContext | None,
    ) -> VerificationAccessContext:
        problem_id = int(verification["problem_id"])
        owner_workspace_raw = verification.get("workspace_id")
        owner_workspace_id = (
            None if owner_workspace_raw is None else int(owner_workspace_raw)
        )
        resource = Resource(
            "verification",
            str(verification["id"]),
            problem_id=problem_id,
            workspace_id=owner_workspace_id,
        )
        effective_problem_access = (
            self.problem_context(problem_id, actor.user_id)
            if problem_access is None
            else problem_access
        )
        matches_problem = problem_id == int(expected_problem_id)
        view_allowed = (
            matches_problem
            and effective_problem_access["can_view_verification"]
        )
        rejudge_allowed = matches_problem and effective_problem_access["can_rejudge"]
        cancel = role_decision(
            actor,
            resource,
            "verification.cancel",
            effective_problem_access["role"],
        )
        cancel_allowed = matches_problem and view_allowed and cancel.allowed
        return {
            "can_view": view_allowed,
            "can_rejudge": rejudge_allowed,
            "can_cancel": cancel_allowed,
            "owns_verification": (
                actor.workspace_id is not None
                and actor.workspace_id == owner_workspace_id
            ),
            "view_block_reason": "" if view_allowed else (
                "verification not found"
                if not matches_problem
                else effective_problem_access["verification_block_reason"]
            ),
            "rejudge_block_reason": "" if rejudge_allowed else (
                "verification not found"
                if not matches_problem
                else effective_problem_access["rejudge_block_reason"]
            ),
            "cancel_block_reason": "" if cancel_allowed else (
                "verification not found" if not matches_problem else cancel.reason
            ),
        }

    def package_job_context(
        self,
        *,
        actor_user_id: int,
        problem_id: int,
        job_actor_user_id: int,
        status: str,
        problem_access: ProblemAccessContext | None = None,
    ) -> PackageJobAccessContext:
        access = (
            self.problem_context(problem_id, actor_user_id)
            if problem_access is None
            else problem_access
        )
        own_or_terminal = (
            int(actor_user_id) == int(job_actor_user_id)
            or status == "succeeded"
        )
        can_view = access["can_list_packages"] and (
            own_or_terminal or access["can_manage"]
        )
        can_download = can_view and status == "succeeded"
        return {
            "can_view": can_view,
            "can_download": can_download,
            "view_block_reason": "" if can_view else "export job is not visible to this user",
            "download_block_reason": "" if can_download else "export artifact is not available",
        }

    def package_export_context(
        self,
        *,
        actor_user_id: int,
        expected_problem_id: int,
        export: Mapping[str, object] | None,
        problem_access: ProblemAccessContext | None = None,
    ) -> PackageJobAccessContext:
        matches_problem = (
            export is not None
            and int(export["problem_id"]) == int(expected_problem_id)
        )
        access = (
            self.problem_context(expected_problem_id, actor_user_id)
            if problem_access is None
            else problem_access
        )
        can_download = matches_problem and access["can_download_packages"]
        return {
            "can_view": can_download,
            "can_download": can_download,
            "view_block_reason": (
                "" if can_download else "export artifact is not available"
            ),
            "download_block_reason": (
                "" if can_download else "export artifact is not available"
            ),
        }

    def package_materialization_context(
        self,
        *,
        actor_user_id: int,
        expected_problem_id: int,
        materialization: Mapping[str, object] | None,
        problem_access: ProblemAccessContext | None = None,
    ) -> PackageJobAccessContext:
        matches_problem = (
            materialization is not None
            and int(materialization["problem_id"]) == int(expected_problem_id)
        )
        access = (
            self.problem_context(expected_problem_id, actor_user_id)
            if problem_access is None
            else problem_access
        )
        can_download = matches_problem and access["can_download_packages"]
        return {
            "can_view": can_download,
            "can_download": can_download,
            "view_block_reason": (
                "" if can_download else "package is not available"
            ),
            "download_block_reason": (
                "" if can_download else "package is not available"
            ),
        }

    def effective_agent_scope(
        self,
        *,
        token_scope: str,
        problem_id: int,
        user_id: int,
    ) -> str:
        role = self.problem_context(problem_id, user_id)["role"]
        return effective_agent_scope(token_scope, role) or ""

    @staticmethod
    def agent_scope_allows(granted_scope: str, minimum_scope: str) -> bool:
        return agent_scope_allows(granted_scope, minimum_scope)

    @staticmethod
    def canonical_agent_scope(scope: str) -> str:
        return agent_scope(scope)
