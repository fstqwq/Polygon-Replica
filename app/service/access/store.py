import sqlite3

from app.db import DB
from app.service.access.model import AccessRole, ProblemParticipationRow
from app.service.access.policy import access_role, derived_problem_role, stronger_role


class AccessStore:
    def __init__(self, db: DB):
        self._db = db

    def is_system_admin(self, user_id: int) -> bool:
        row = self._db.fetch_one(
            "SELECT is_system_admin FROM users WHERE id=?",
            [int(user_id)],
        )
        return row is not None and int(row["is_system_admin"] or 0) == 1

    def direct_problem_role(self, problem_id: int, user_id: int) -> AccessRole:
        return self.direct_problem_roles([problem_id], user_id)[int(problem_id)]

    def direct_problem_roles(
        self,
        problem_ids: list[int],
        user_id: int,
    ) -> dict[int, AccessRole]:
        ids = sorted({int(problem_id) for problem_id in problem_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _problem_id in ids)
        rows = self._db.fetch_all(
            f"""
            SELECT problem_id,role
            FROM repo_acl
            WHERE user_id=? AND problem_id IN ({placeholders})
            """,
            [int(user_id), *ids],
        )
        result = {problem_id: access_role(None) for problem_id in ids}
        for row in rows:
            result[int(row["problem_id"])] = access_role(str(row["role"]))
        return result

    def effective_problem_roles(
        self,
        problem_ids: list[int],
        user_id: int,
    ) -> dict[int, AccessRole]:
        ids = sorted({int(problem_id) for problem_id in problem_ids})
        if not ids:
            return {}
        if self.is_system_admin(user_id):
            return {problem_id: "admin" for problem_id in ids}
        placeholders = ",".join("?" for _problem_id in ids)
        rows = self._db.fetch_all(
            f"""
            SELECT problem_id,role,'direct' AS source
            FROM repo_acl
            WHERE user_id=? AND problem_id IN ({placeholders})
            UNION ALL
            SELECT cp.problem_id,cm.role,'contest' AS source
            FROM contest_problems cp
            JOIN contest_members cm ON cm.contest_id=cp.contest_id
            WHERE cm.user_id=? AND cp.problem_id IN ({placeholders})
            """,
            [int(user_id), *ids, int(user_id), *ids],
        )
        result = {problem_id: access_role(None) for problem_id in ids}
        for row in rows:
            problem_id = int(row["problem_id"])
            role = access_role(str(row["role"]))
            if str(row["source"]) == "contest":
                role = derived_problem_role(role)
            result[problem_id] = stronger_role(result[problem_id], role)
        return result

    @staticmethod
    def problem_role_in_transaction(
        connection: sqlite3.Connection,
        *,
        problem_id: int,
        user_id: int,
    ) -> AccessRole:
        user = connection.execute(
            "SELECT is_system_admin FROM users WHERE id=?",
            [int(user_id)],
        ).fetchone()
        if user is None:
            return "none"
        if int(user[0] or 0) == 1:
            return "admin"
        rows = connection.execute(
            """
            SELECT role,'direct' AS source
            FROM repo_acl
            WHERE problem_id=? AND user_id=?
            UNION ALL
            SELECT cm.role,'contest' AS source
            FROM contest_problems cp
            JOIN contest_members cm ON cm.contest_id=cp.contest_id
            WHERE cp.problem_id=? AND cm.user_id=?
            """,
            [int(problem_id), int(user_id), int(problem_id), int(user_id)],
        ).fetchall()
        role: AccessRole = "none"
        for row in rows:
            candidate = access_role(str(row[0]))
            if str(row[1]) == "contest":
                candidate = derived_problem_role(candidate)
            role = stronger_role(role, candidate)
        return role

    def contest_role(self, contest_id: int, user_id: int) -> AccessRole:
        if self.is_system_admin(user_id):
            return "admin"
        row = self._db.fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=?",
            [int(contest_id), int(user_id)],
        )
        return access_role(None if row is None else str(row["role"]))

    def workspace_belongs_to_user(self, workspace_id: int, user_id: int) -> bool:
        row = self._db.fetch_one(
            "SELECT 1 FROM workspaces WHERE id=? AND user_id=?",
            [int(workspace_id), int(user_id)],
        )
        return row is not None

    def accessible_problem_slugs(self, user_id: int, *, limit: int) -> list[str]:
        rows = self._db.fetch_all(
            """
            WITH accessible(problem_id) AS (
                SELECT problem_id FROM repo_acl WHERE user_id=?
                UNION
                SELECT cp.problem_id
                FROM contest_problems cp
                JOIN contest_members cm ON cm.contest_id=cp.contest_id
                WHERE cm.user_id=?
            )
            SELECT p.slug
            FROM accessible a
            JOIN problems p ON p.id=a.problem_id
            LEFT JOIN workspaces w ON w.problem_id=p.id AND w.user_id=?
            ORDER BY COALESCE(NULLIF(w.updated_at, ''), p.created_at) DESC,
                     p.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), int(user_id), max(1, int(limit))],
        )
        return [str(row["slug"]) for row in rows]

    def all_problem_slugs(self, *, limit: int) -> list[str]:
        rows = self._db.fetch_all(
            """
            SELECT slug FROM problems
            ORDER BY created_at DESC, slug ASC
            LIMIT ?
            """,
            [max(1, int(limit))],
        )
        return [str(row["slug"]) for row in rows]

    def all_problem_slugs_by_leaf(self, leaf: str, *, limit: int) -> list[str]:
        rows = self._db.fetch_all(
            """
            SELECT slug FROM problems
            WHERE slug LIKE ?
            ORDER BY slug ASC
            LIMIT ?
            """,
            [f"%/{leaf}", max(1, int(limit))],
        )
        return [str(row["slug"]) for row in rows]

    def accessible_problem_slugs_by_leaf(
        self,
        user_id: int,
        leaf: str,
        *,
        limit: int,
    ) -> list[str]:
        rows = self._db.fetch_all(
            """
            WITH accessible(problem_id) AS (
                SELECT problem_id FROM repo_acl WHERE user_id=?
                UNION
                SELECT cp.problem_id
                FROM contest_problems cp
                JOIN contest_members cm ON cm.contest_id=cp.contest_id
                WHERE cm.user_id=?
            )
            SELECT p.slug
            FROM accessible a
            JOIN problems p ON p.id=a.problem_id
            WHERE p.slug LIKE ?
            ORDER BY p.slug ASC
            LIMIT ?
            """,
            [
                int(user_id),
                int(user_id),
                f"%/{leaf}",
                max(1, int(limit)),
            ],
        )
        return [str(row["slug"]) for row in rows]

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise RuntimeError("database integer field has invalid shape")
        return value

    def participating_problem_rows(
        self,
        user_id: int,
        *,
        limit: int,
    ) -> list[ProblemParticipationRow]:
        if self.is_system_admin(user_id):
            rows = self._db.fetch_all(
                """
                SELECT p.id,p.slug,'admin' AS role,
                       w.id AS workspace_id,w.path,w.branch,w.head_commit,
                       w.dirty,w.updated_at,w.revision_local,
                       w.revision_upstream,w.revision_missing,
                       w.revision_highlight,w.revision_upstream_higher,
                       w.revision_ahead_count,w.revision_behind_count,
                       COALESCE(NULLIF(w.updated_at, ''), p.created_at)
                           AS last_updated_at
                FROM problems p
                LEFT JOIN workspaces w
                  ON w.problem_id=p.id AND w.user_id=?
                ORDER BY last_updated_at DESC, p.slug ASC
                LIMIT ?
                """,
                [int(user_id), max(1, int(limit))],
            )
        else:
            rows = self._db.fetch_all(
                """
                WITH grants(problem_id,role_rank) AS (
                    SELECT problem_id,
                           CASE role
                               WHEN 'owner' THEN 3
                               WHEN 'write' THEN 2
                               ELSE 1
                           END
                    FROM repo_acl WHERE user_id=?
                    UNION ALL
                    SELECT cp.problem_id,
                           CASE WHEN cm.role IN ('owner','write') THEN 2 ELSE 1 END
                    FROM contest_problems cp
                    JOIN contest_members cm ON cm.contest_id=cp.contest_id
                    WHERE cm.user_id=?
                ), effective AS (
                    SELECT problem_id,MAX(role_rank) AS role_rank
                    FROM grants GROUP BY problem_id
                )
                SELECT p.id,p.slug,
                       CASE e.role_rank
                           WHEN 3 THEN 'owner'
                           WHEN 2 THEN 'write'
                           ELSE 'read'
                       END AS role,
                       w.id AS workspace_id,w.path,w.branch,w.head_commit,
                       w.dirty,w.updated_at,w.revision_local,
                       w.revision_upstream,w.revision_missing,
                       w.revision_highlight,w.revision_upstream_higher,
                       w.revision_ahead_count,w.revision_behind_count,
                       COALESCE(NULLIF(w.updated_at, ''), p.created_at)
                           AS last_updated_at
                FROM effective e
                JOIN problems p ON p.id=e.problem_id
                LEFT JOIN workspaces w
                  ON w.problem_id=p.id AND w.user_id=?
                ORDER BY last_updated_at DESC, p.slug ASC
                LIMIT ?
                """,
                [int(user_id), int(user_id), int(user_id), max(1, int(limit))],
            )
        result: list[ProblemParticipationRow] = []
        for row in rows:
            workspace_id = row["workspace_id"]
            result.append(
                {
                    "slug": str(row["slug"]),
                    "role": access_role(str(row["role"])),
                    "workspace_id": (None if workspace_id is None else int(workspace_id)),
                    "path": str(row["path"] or ""),
                    "branch": str(row["branch"] or ""),
                    "head_commit": str(row["head_commit"] or ""),
                    "dirty": int(row["dirty"] or 0),
                    "revision_local": self._optional_int(row["revision_local"]),
                    "revision_upstream": self._optional_int(row["revision_upstream"]),
                    "revision_missing": int(
                        row["revision_missing"] if row["revision_missing"] is not None else 1
                    ),
                    "revision_highlight": int(
                        row["revision_highlight"] if row["revision_highlight"] is not None else 1
                    ),
                    "revision_upstream_higher": int(row["revision_upstream_higher"] or 0),
                    "revision_ahead_count": self._optional_int(row["revision_ahead_count"]),
                    "revision_behind_count": self._optional_int(row["revision_behind_count"]),
                    "updated_at": str(row["updated_at"] or ""),
                    "last_updated_at": str(row["last_updated_at"] or ""),
                }
            )
        return result

    def directly_writable_problem_rows_excluding_contest(
        self,
        contest_id: int,
        user_id: int,
        *,
        limit: int,
    ) -> list[dict[str, object]]:
        if self.is_system_admin(user_id):
            rows = self._db.fetch_all(
                """
                SELECT p.id AS problem_id,p.slug AS problem_slug,'admin' AS role
                FROM problems p
                WHERE NOT EXISTS (
                    SELECT 1 FROM contest_problems cp
                    WHERE cp.contest_id=? AND cp.problem_id=p.id
                )
                ORDER BY p.slug ASC LIMIT ?
                """,
                [int(contest_id), max(1, int(limit))],
            )
        else:
            rows = self._db.fetch_all(
                """
                SELECT p.id AS problem_id,p.slug AS problem_slug,a.role
                FROM repo_acl a
                JOIN problems p ON p.id=a.problem_id
                WHERE a.user_id=? AND a.role IN ('write','owner')
                  AND NOT EXISTS (
                      SELECT 1 FROM contest_problems cp
                      WHERE cp.contest_id=? AND cp.problem_id=p.id
                  )
                ORDER BY p.slug ASC LIMIT ?
                """,
                [int(user_id), int(contest_id), max(1, int(limit))],
            )
        return [dict(row) for row in rows]
