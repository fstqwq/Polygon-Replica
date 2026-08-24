"""Transactional mutations for direct Problem and Contest access."""

import sqlite3
from typing import TypedDict

from app.db import DB, now_iso
from app.service.access.errors import AccessConflictError
from app.service.access.model import (
    AccessMutationResult,
    AccessRole,
    ProblemAccessChange,
)
from app.service.access.policy import access_role, contest_role, repo_role
from app.service.access.store import AccessStore


class _AccessTarget(TypedDict):
    id: int
    username: str
    is_system_admin: int


class AccessCommand:
    def __init__(self, db: DB):
        self._db = db

    @staticmethod
    def _user_by_username(
        connection: sqlite3.Connection,
        username: str,
    ) -> _AccessTarget | None:
        row = connection.execute(
            """
            SELECT id,username,COALESCE(is_system_admin, 0) AS is_system_admin
            FROM users
            WHERE LOWER(username)=LOWER(?)
            ORDER BY id ASC
            LIMIT 1
            """,
            [username],
        ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "is_system_admin": int(row["is_system_admin"] or 0),
        }

    @staticmethod
    def _require_problem_access_manager(
        connection: sqlite3.Connection,
        *,
        problem_id: int,
        actor_user_id: int,
    ) -> AccessRole:
        problem = connection.execute(
            "SELECT 1 FROM problems WHERE id=?",
            [int(problem_id)],
        ).fetchone()
        if problem is None:
            raise ValueError("problem not found")
        role = AccessStore.problem_role_in_transaction(
            connection,
            problem_id=int(problem_id),
            user_id=int(actor_user_id),
        )
        if role not in {"write", "owner", "admin"}:
            raise PermissionError(
                "read-only access" if role == "read" else "write access required"
            )
        return role

    @staticmethod
    def _require_contest_access_manager(
        connection: sqlite3.Connection,
        *,
        contest_id: int,
        actor_user_id: int,
    ) -> AccessRole:
        contest = connection.execute(
            "SELECT 1 FROM contests WHERE id=?",
            [int(contest_id)],
        ).fetchone()
        if contest is None:
            raise ValueError("contest not found")
        role = AccessStore.contest_role_in_transaction(
            connection,
            contest_id=int(contest_id),
            user_id=int(actor_user_id),
        )
        if role not in {"write", "owner", "admin"}:
            raise PermissionError(
                "read-only access" if role == "read" else "write access required"
            )
        return role

    @staticmethod
    def _current_problem_role(
        connection: sqlite3.Connection,
        *,
        problem_id: int,
        target_user_id: int,
    ) -> AccessRole:
        row = connection.execute(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [int(problem_id), int(target_user_id)],
        ).fetchone()
        return access_role(None if row is None else str(row[0]))

    @staticmethod
    def _require_mutable_problem_target(
        connection: sqlite3.Connection,
        *,
        problem_id: int,
        actor_user_id: int,
        target_user_id: int,
        target_is_system_admin: bool,
    ) -> AccessRole:
        if int(target_user_id) == int(actor_user_id):
            raise ValueError("you cannot change your own problem access")
        if target_is_system_admin:
            raise ValueError("system administrator access is fixed")
        existing_role = AccessCommand._current_problem_role(
            connection,
            problem_id=int(problem_id),
            target_user_id=int(target_user_id),
        )
        if existing_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")
        return existing_role

    def set_problem_access(
        self,
        *,
        actor_user_id: int,
        problem_id: int,
        target_username: str,
        role: str,
    ) -> AccessMutationResult:
        safe_role = repo_role(role)
        if safe_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")

        def tx(connection: sqlite3.Connection) -> AccessMutationResult:
            self._require_problem_access_manager(
                connection,
                problem_id=int(problem_id),
                actor_user_id=int(actor_user_id),
            )
            target = self._user_by_username(connection, target_username)
            if target is None:
                raise ValueError(
                    f"user {target_username} not found; ask them to register first"
                )
            target_user_id = int(target["id"])
            previous_role = self._require_mutable_problem_target(
                connection,
                problem_id=int(problem_id),
                actor_user_id=int(actor_user_id),
                target_user_id=target_user_id,
                target_is_system_admin=int(target["is_system_admin"] or 0) == 1,
            )
            connection.execute(
                """
                INSERT INTO repo_acl(problem_id,user_id,role,created_at)
                VALUES(?,?,?,?)
                ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role
                """,
                [int(problem_id), target_user_id, safe_role, now_iso()],
            )
            return {
                "target_user_id": target_user_id,
                "target_username": str(target["username"]),
                "previous_role": "" if previous_role == "none" else previous_role,
                "role": safe_role,
            }

        return self._db.write_transaction(tx)

    def revoke_problem_access(
        self,
        *,
        actor_user_id: int,
        problem_id: int,
        target_username: str,
    ) -> AccessMutationResult:
        def tx(connection: sqlite3.Connection) -> AccessMutationResult:
            self._require_problem_access_manager(
                connection,
                problem_id=int(problem_id),
                actor_user_id=int(actor_user_id),
            )
            target = self._user_by_username(connection, target_username)
            if target is None:
                raise ValueError("access entry not found")
            target_user_id = int(target["id"])
            previous_role = self._require_mutable_problem_target(
                connection,
                problem_id=int(problem_id),
                actor_user_id=int(actor_user_id),
                target_user_id=target_user_id,
                target_is_system_admin=int(target["is_system_admin"] or 0) == 1,
            )
            if previous_role == "none":
                raise ValueError("access entry not found")
            connection.execute(
                "DELETE FROM repo_acl WHERE problem_id=? AND user_id=?",
                [int(problem_id), target_user_id],
            )
            return {
                "target_user_id": target_user_id,
                "target_username": str(target["username"]),
                "previous_role": previous_role,
                "role": "none",
            }

        return self._db.write_transaction(tx)

    def set_contest_membership(
        self,
        *,
        actor_user_id: int,
        contest_id: int,
        target_username: str,
        role: str,
    ) -> AccessMutationResult:
        safe_role = contest_role(role)
        if safe_role == "owner":
            raise ValueError("owner access is fixed and cannot be transferred")

        def tx(connection: sqlite3.Connection) -> AccessMutationResult:
            self._require_contest_access_manager(
                connection,
                contest_id=int(contest_id),
                actor_user_id=int(actor_user_id),
            )
            target = self._user_by_username(connection, target_username)
            if target is None:
                raise ValueError(
                    f"user {target_username} not found; ask them to register first"
                )
            target_user_id = int(target["id"])
            if target_user_id == int(actor_user_id):
                raise ValueError("you cannot change your own contest membership")
            contest = connection.execute(
                "SELECT owner_user_id FROM contests WHERE id=?",
                [int(contest_id)],
            ).fetchone()
            existing = connection.execute(
                "SELECT role FROM contest_members WHERE contest_id=? AND user_id=?",
                [int(contest_id), target_user_id],
            ).fetchone()
            previous_role = "none" if existing is None else contest_role(str(existing[0]))
            if (
                contest is None
                or target_user_id == int(contest[0])
                or previous_role == "owner"
            ):
                raise ValueError("owner access is fixed and cannot be transferred")
            connection.execute(
                """
                INSERT INTO contest_members(contest_id,user_id,role,created_at)
                VALUES(?,?,?,?)
                ON CONFLICT(contest_id,user_id) DO UPDATE SET role=excluded.role
                """,
                [int(contest_id), target_user_id, safe_role, now_iso()],
            )
            return {
                "target_user_id": target_user_id,
                "target_username": str(target["username"]),
                "previous_role": previous_role,
                "role": safe_role,
            }

        return self._db.write_transaction(tx)

    def revoke_contest_membership(
        self,
        *,
        actor_user_id: int,
        contest_id: int,
        target_username: str,
    ) -> AccessMutationResult:
        def tx(connection: sqlite3.Connection) -> AccessMutationResult:
            target = self._user_by_username(connection, target_username)
            is_self = bool(
                target is not None
                and int(target["id"]) == int(actor_user_id)
            )
            if not is_self:
                self._require_contest_access_manager(
                    connection,
                    contest_id=int(contest_id),
                    actor_user_id=int(actor_user_id),
                )
            if target is None:
                raise ValueError(f"{target_username} is not a member")
            target_user_id = int(target["id"])
            contest = connection.execute(
                "SELECT owner_user_id FROM contests WHERE id=?",
                [int(contest_id)],
            ).fetchone()
            existing = connection.execute(
                "SELECT role FROM contest_members WHERE contest_id=? AND user_id=?",
                [int(contest_id), target_user_id],
            ).fetchone()
            if existing is None:
                raise ValueError(f"{target_username} is not a member")
            previous_role = contest_role(str(existing[0]))
            if (
                contest is None
                or target_user_id == int(contest[0])
                or previous_role == "owner"
            ):
                raise ValueError("owner access is fixed and cannot be transferred")
            connection.execute(
                "DELETE FROM contest_members WHERE contest_id=? AND user_id=?",
                [int(contest_id), target_user_id],
            )
            return {
                "target_user_id": target_user_id,
                "target_username": str(target["username"]),
                "previous_role": previous_role,
                "role": "none",
            }

        return self._db.write_transaction(tx)

    def save_contest_problem_access(
        self,
        *,
        actor_user_id: int,
        contest_id: int,
        changes: list[ProblemAccessChange],
    ) -> int:
        pairs = [
            (int(change.problem_id), int(change.target_user_id))
            for change in changes
        ]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate problem access cell")
        for change in changes:
            if change.problem_id <= 0 or change.target_user_id <= 0:
                raise ValueError("invalid problem access cell")
            if change.original_role not in {"none", "read", "write"}:
                raise ValueError("invalid original problem role")
            if change.requested_role not in {"none", "read", "write"}:
                raise ValueError("invalid requested problem role")

        def tx(connection: sqlite3.Connection) -> int:
            actor_contest_role = self._require_contest_access_manager(
                connection,
                contest_id=int(contest_id),
                actor_user_id=int(actor_user_id),
            )
            actor_is_admin = actor_contest_role == "admin"
            changed_count = 0
            for change in changes:
                roster = connection.execute(
                    """
                    SELECT 1 FROM contest_problems
                    WHERE contest_id=? AND problem_id=?
                    """,
                    [int(contest_id), int(change.problem_id)],
                ).fetchone()
                if roster is None:
                    raise AccessConflictError(
                        "a selected problem is no longer part of this contest"
                    )
                target = connection.execute(
                    """
                    SELECT u.username,COALESCE(u.is_system_admin, 0) AS is_system_admin
                    FROM contest_members m
                    JOIN users u ON u.id=m.user_id
                    WHERE m.contest_id=? AND m.user_id=?
                    """,
                    [int(contest_id), int(change.target_user_id)],
                ).fetchone()
                if target is None:
                    raise AccessConflictError(
                        "a selected user is no longer a contest member"
                    )
                if not actor_is_admin:
                    self._require_problem_access_manager(
                        connection,
                        problem_id=int(change.problem_id),
                        actor_user_id=int(actor_user_id),
                    )
                current_role = self._current_problem_role(
                    connection,
                    problem_id=int(change.problem_id),
                    target_user_id=int(change.target_user_id),
                )
                if current_role != change.original_role:
                    raise AccessConflictError(
                        "problem access changed after this page was loaded"
                    )
                self._require_mutable_problem_target(
                    connection,
                    problem_id=int(change.problem_id),
                    actor_user_id=int(actor_user_id),
                    target_user_id=int(change.target_user_id),
                    target_is_system_admin=int(target["is_system_admin"] or 0) == 1,
                )
                if change.requested_role == current_role:
                    continue
                if change.requested_role == "none":
                    connection.execute(
                        "DELETE FROM repo_acl WHERE problem_id=? AND user_id=?",
                        [int(change.problem_id), int(change.target_user_id)],
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO repo_acl(problem_id,user_id,role,created_at)
                        VALUES(?,?,?,?)
                        ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role
                        """,
                        [
                            int(change.problem_id),
                            int(change.target_user_id),
                            change.requested_role,
                            now_iso(),
                        ],
                    )
                changed_count += 1
            return changed_count

        return int(self._db.write_transaction(tx))
