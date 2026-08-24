import sqlite3
from typing import TypedDict

from app.db import DB, now_iso
from app.service.access.model import ProblemAclEntry
from app.service.verification.task_store import VerificationTaskStore
from app.service.workspace.state import WorkspaceState


class ProblemRow(TypedDict):
    id: int
    slug: str
    repo_name: str
    created_at: str


class UserRow(TypedDict):
    id: int
    username: str
    created_at: str
    is_system_admin: int
    is_banned: int


class WorkspaceIdentityRow(TypedDict):
    id: int
    problem_id: int
    user_id: int
    path: str


class WorkspaceRecentVerificationRow(TypedDict):
    id: str
    status: str
    created_at: str


class UserContestOverviewRow(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    created_at: str
    role: str
    last_updated_at: str
    problem_count: int
    problem_slugs_preview: str
    dirty_problem_count: int


class WorkspaceDiskStore:
    def __init__(self, db: DB, *, verification_task_store: VerificationTaskStore):
        self.db = db
        self.verification_task_store = verification_task_store

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("SQLite integer column has a non-integer value")
        return value

    def problem_id_by_slug(self, slug: str) -> int | None:
        row = self.db.fetch_one("SELECT id FROM problems WHERE slug=?", [slug])
        if row is None:
            return None
        return int(row["id"])

    def problem_row_by_slug(self, slug: str) -> ProblemRow | None:
        row = self.db.fetch_one(
            "SELECT id,slug,repo_name,created_at FROM problems WHERE slug=?",
            [slug],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"] or ""),
            "repo_name": str(row["repo_name"] or ""),
            "created_at": str(row["created_at"] or ""),
        }

    def problem_row_by_id_slug(self, problem_id: int, slug: str) -> ProblemRow | None:
        row = self.db.fetch_one(
            "SELECT id,slug,repo_name,created_at FROM problems WHERE id=? AND slug=?",
            [int(problem_id), slug],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"] or ""),
            "repo_name": str(row["repo_name"] or ""),
            "created_at": str(row["created_at"] or ""),
        }

    def ensure_problem_row(self, *, slug: str, repo_name: str) -> ProblemRow:
        self.db.execute(
            "INSERT OR IGNORE INTO problems(slug, repo_name, created_at) VALUES(?,?,?)",
            [slug, repo_name, now_iso()],
        )
        row = self.problem_row_by_slug(slug)
        if row is None:
            raise RuntimeError(f"unable to ensure problem row for {slug}")
        return row

    def user_id_by_username(self, username: str) -> int | None:
        row = self.db.fetch_one(
            "SELECT id FROM users WHERE LOWER(username)=LOWER(?) ORDER BY id ASC LIMIT 1",
            [username],
        )
        if row is None:
            return None
        return int(row["id"])

    def user_row_by_username(self, username: str) -> UserRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,username,created_at,is_system_admin,COALESCE(is_banned, 0) AS is_banned
            FROM users
            WHERE LOWER(username)=LOWER(?)
            ORDER BY id ASC
            LIMIT 1
            """,
            [username],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"] or ""),
            "created_at": str(row["created_at"] or ""),
            "is_system_admin": int(row["is_system_admin"] or 0),
            "is_banned": int(row["is_banned"] or 0),
        }

    def user_row_by_id_username(self, user_id: int, username: str) -> UserRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,username,created_at,is_system_admin,COALESCE(is_banned, 0) AS is_banned
            FROM users
            WHERE id=? AND username=?
            """,
            [int(user_id), username],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"] or ""),
            "created_at": str(row["created_at"] or ""),
            "is_system_admin": int(row["is_system_admin"] or 0),
            "is_banned": int(row["is_banned"] or 0),
        }

    def ensure_user_row(self, username: str) -> UserRow:
        existing = self.user_row_by_username(username)
        if existing is not None:
            return existing
        self.db.execute(
            "INSERT OR IGNORE INTO users(username, created_at) VALUES(?,?)",
            [username, now_iso()],
        )
        row = self.user_row_by_username(username)
        if row is None:
            raise RuntimeError(f"unable to ensure user row for {username}")
        return row

    def problem_owner_count(self, problem_id: int) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM repo_acl WHERE problem_id=? AND role='owner'",
            [problem_id],
        )
        if row is None:
            return 0
        return max(0, int(row["c"]))

    def repo_access_row(self, problem_id: int, user_id: int) -> dict[str, object] | None:
        row = self.db.fetch_one(
            "SELECT role FROM repo_acl WHERE problem_id=? AND user_id=?",
            [int(problem_id), int(user_id)],
        )
        if row is None:
            return None
        return {"role": str(row["role"])}

    def upsert_repo_access(self, problem_id: int, user_id: int, role: str) -> None:
        self.db.execute(
            """
            INSERT INTO repo_acl(problem_id,user_id,role,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(problem_id,user_id) DO UPDATE SET role=excluded.role
            """,
            [int(problem_id), int(user_id), role, now_iso()],
        )

    def delete_repo_access(self, problem_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM repo_acl WHERE problem_id=? AND user_id=?",
            [int(problem_id), int(user_id)],
        )

    def problem_acl_entries(self, problem_id: int) -> list[ProblemAclEntry]:
        rows = self.db.fetch_all(
            """
            SELECT u.id AS user_id,u.username,a.role,a.created_at,
                   COALESCE(u.is_system_admin, 0) AS is_system_admin
            FROM repo_acl a
            JOIN users u ON u.id=a.user_id
            WHERE a.problem_id=?
            ORDER BY
                CASE a.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
                u.username ASC
            """,
            [problem_id],
        )
        entries: list[ProblemAclEntry] = []
        for row in rows:
            entries.append(
                {
                    "user_id": int(row["user_id"]),
                    "username": str(row["username"]),
                    "role": str(row["role"]),
                    "created_at": str(row["created_at"]),
                    "is_system_admin": int(row["is_system_admin"] or 0),
                }
            )
        return entries

    def problem_slugs_by_leaf(self, leaf: str, *, limit: int) -> list[str]:
        rows = self.db.fetch_all(
            """
            SELECT slug
            FROM problems
            WHERE slug LIKE ?
            ORDER BY slug ASC
            LIMIT ?
            """,
            [f"%/{str(leaf or '').strip()}", max(1, int(limit))],
        )
        result: list[str] = []
        for row in rows:
            slug = str(row["slug"] or "")
            if slug:
                result.append(slug)
        return result

    def user_contest_rows(self, user_id: int, *, limit: int) -> list[UserContestOverviewRow]:
        rows = self.db.fetch_all(
            """
            SELECT c.id,c.slug,COALESCE(title_property.value, '') AS title,
                   c.owner_user_id,c.created_at,m.role,
                   MAX(
                       c.created_at,
                       COALESCE((SELECT MAX(cp0.created_at) FROM contest_problems cp0 WHERE cp0.contest_id=c.id), ''),
                       COALESCE((
                           SELECT MAX(w2.updated_at)
                           FROM contest_problems cp2
                           JOIN workspaces w2 ON w2.problem_id=cp2.problem_id AND w2.user_id=?
                           WHERE cp2.contest_id=c.id
                       ), '')
                   ) AS last_updated_at,
                   (
                       SELECT COUNT(*)
                       FROM contest_problems cp
                       WHERE cp.contest_id=c.id
                   ) AS problem_count,
                   (
                       SELECT group_concat(x.slug, ', ')
                       FROM (
                           SELECT p.slug AS slug
                           FROM contest_problems cp
                           JOIN problems p ON p.id=cp.problem_id
                           WHERE cp.contest_id=c.id
                           ORDER BY p.slug ASC
                           LIMIT 5
                       ) x
                   ) AS problem_slugs_preview,
                   (
                       SELECT COUNT(*)
                       FROM contest_problems cp3
                       JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?
                       WHERE cp3.contest_id=c.id
                         AND COALESCE(w.dirty, 0) <> 0
                   ) AS dirty_problem_count
            FROM contests c
            JOIN contest_members m ON m.contest_id=c.id
            LEFT JOIN contest_properties title_property
              ON title_property.contest_id=c.id AND title_property.key='title'
            WHERE m.user_id=?
            ORDER BY last_updated_at DESC, c.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), int(user_id), max(1, int(limit))],
        )
        items: list[UserContestOverviewRow] = []
        for row in rows:
            items.append(
                {
                    "id": int(row["id"]),
                    "slug": str(row["slug"] or ""),
                    "title": str(row["title"] or ""),
                    "owner_user_id": int(row["owner_user_id"]),
                    "created_at": str(row["created_at"] or ""),
                    "role": str(row["role"] or ""),
                    "last_updated_at": str(row["last_updated_at"] or ""),
                    "problem_count": int(row["problem_count"] or 0),
                    "problem_slugs_preview": str(row["problem_slugs_preview"] or ""),
                    "dirty_problem_count": int(row["dirty_problem_count"] or 0),
                }
            )
        return items

    def workspace_path(self, problem_id: int, workspace_id: int) -> str:
        row = self.db.fetch_one(
            "SELECT path FROM workspaces WHERE id=? AND problem_id=?",
            [int(workspace_id), int(problem_id)],
        )
        if row is None:
            return ""
        return str(row["path"] or "")

    def workspace_id(self, problem_id: int, user_id: int) -> int | None:
        row = self.db.fetch_one(
            "SELECT id FROM workspaces WHERE problem_id=? AND user_id=?",
            [int(problem_id), int(user_id)],
        )
        if row is None:
            return None
        return int(row["id"])

    def _workspace_record(self, row) -> WorkspaceState:
        return {
            "id": int(row["id"]),
            "problem_id": int(row["problem_id"]),
            "user_id": int(row["user_id"]),
            "path": str(row["path"] or ""),
            "branch": str(row["branch"] or ""),
            "head_commit": str(row["head_commit"] or ""),
            "dirty": int(row["dirty"] or 0),
            "revision_local": self._optional_int(row["revision_local"]),
            "revision_upstream": self._optional_int(row["revision_upstream"]),
            "revision_missing": int(
                row["revision_missing"]
                if row["revision_missing"] is not None
                else 1
            ),
            "revision_highlight": int(
                row["revision_highlight"]
                if row["revision_highlight"] is not None
                else 1
            ),
            "revision_upstream_higher": int(
                row["revision_upstream_higher"] or 0
            ),
            "revision_ahead_count": self._optional_int(
                row["revision_ahead_count"]
            ),
            "revision_behind_count": self._optional_int(
                row["revision_behind_count"]
            ),
            "updated_at": str(row["updated_at"] or ""),
        }

    def workspace_row(self, problem_id: int, user_id: int) -> WorkspaceState | None:
        row = self.db.fetch_one(
            """
            SELECT id,problem_id,user_id,path,branch,head_commit,dirty,
                   revision_local,revision_upstream,revision_missing,revision_highlight,
                   revision_upstream_higher,revision_ahead_count,revision_behind_count,
                   updated_at
            FROM workspaces
            WHERE problem_id=? AND user_id=?
            """,
            [int(problem_id), int(user_id)],
        )
        if row is None:
            return None
        return self._workspace_record(row)

    def workspace_rows(
        self,
        problem_ids: list[int],
        user_id: int,
    ) -> dict[int, WorkspaceState]:
        unique_problem_ids = list(dict.fromkeys(int(value) for value in problem_ids))
        if not unique_problem_ids:
            return {}
        placeholders = ",".join("?" for _ in unique_problem_ids)
        rows = self.db.fetch_all(
            f"""
            SELECT id,problem_id,user_id,path,branch,head_commit,dirty,
                   revision_local,revision_upstream,revision_missing,revision_highlight,
                   revision_upstream_higher,revision_ahead_count,revision_behind_count,
                   updated_at
            FROM workspaces
            WHERE user_id=? AND problem_id IN ({placeholders})
            """,
            [int(user_id), *unique_problem_ids],
        )
        return {
            int(row["problem_id"]): self._workspace_record(row)
            for row in rows
        }

    def workspace_identity_by_path(self, path: str) -> WorkspaceIdentityRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,problem_id,user_id,path
            FROM workspaces
            WHERE path=?
            """,
            [path],
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "problem_id": int(row["problem_id"]),
            "user_id": int(row["user_id"]),
            "path": str(row["path"] or ""),
        }

    def ensure_workspace_row(self, problem_id: int, user_id: int, path: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO workspaces(problem_id,user_id,path,updated_at) VALUES(?,?,?,?)",
            [int(problem_id), int(user_id), path, now_iso()],
        )

    def update_workspace_path(self, problem_id: int, user_id: int, path: str) -> None:
        self.db.execute(
            "UPDATE workspaces SET path=?, updated_at=? WHERE problem_id=? AND user_id=? AND path IS NOT ?",
            [path, now_iso(), int(problem_id), int(user_id), path],
        )

    def update_workspace_status(
        self,
        problem_id: int,
        user_id: int,
        *,
        branch: str,
        head_commit: str,
        dirty: int,
        revision_local: int | None,
        revision_upstream: int | None,
        revision_missing: int,
        revision_highlight: int,
        revision_upstream_higher: int,
        revision_ahead_count: int | None,
        revision_behind_count: int | None,
    ) -> None:
        now_text = now_iso()
        self.db.execute(
            """
            UPDATE workspaces
            SET branch=?,
                head_commit=?,
                dirty=?,
                revision_local=?,
                revision_upstream=?,
                revision_missing=?,
                revision_highlight=?,
                revision_upstream_higher=?,
                revision_ahead_count=?,
                revision_behind_count=?,
                updated_at=?
            WHERE problem_id=? AND user_id=?
              AND (
                branch IS NOT ?
                OR head_commit IS NOT ?
                OR dirty IS NOT ?
                OR revision_local IS NOT ?
                OR revision_upstream IS NOT ?
                OR revision_missing IS NOT ?
                OR revision_highlight IS NOT ?
                OR revision_upstream_higher IS NOT ?
                OR revision_ahead_count IS NOT ?
                OR revision_behind_count IS NOT ?
              )
            """,
            [
                branch,
                head_commit,
                int(dirty),
                revision_local,
                revision_upstream,
                int(revision_missing),
                int(revision_highlight),
                int(revision_upstream_higher),
                revision_ahead_count,
                revision_behind_count,
                now_text,
                int(problem_id),
                int(user_id),
                branch,
                head_commit,
                int(dirty),
                revision_local,
                revision_upstream,
                int(revision_missing),
                int(revision_highlight),
                int(revision_upstream_higher),
                revision_ahead_count,
                revision_behind_count,
            ],
        )

    def reset_workspace_row(self, workspace_id: int, path: str) -> None:
        self.db.execute(
            """
            UPDATE workspaces
            SET path=?,
                branch=NULL,
                head_commit=NULL,
                dirty=0,
                revision_local=NULL,
                revision_upstream=NULL,
                revision_missing=1,
                revision_highlight=1,
                revision_upstream_higher=0,
                revision_ahead_count=NULL,
                revision_behind_count=NULL,
                updated_at=?
            WHERE id=?
            """,
            [path, now_iso(), int(workspace_id)],
        )

    def latest_workspace_artifact_verification(self, workspace_id: int) -> WorkspaceRecentVerificationRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,created_at
            FROM verifications
            WHERE workspace_id=?
              AND kind IN ('all','custom')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [int(workspace_id)],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"] or ""),
            "status": str(row["status"] or ""),
            "created_at": str(row["created_at"] or ""),
        }

    def latest_workspace_job_status(self, workspace_id: int, *, kind: str) -> str:
        if kind == "verification":
            row = self.db.fetch_one(
                "SELECT status FROM verifications WHERE workspace_id=? AND kind IN ('all','custom') ORDER BY created_at DESC LIMIT 1",
                [int(workspace_id)],
            )
        else:
            row = self.db.fetch_one(
                "SELECT status FROM verifications WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
                [int(workspace_id)],
            )
        if row is None:
            return ""
        return str(row["status"] or "")

    def workspace_rows_for_problem(self, problem_id: int) -> list[dict[str, object]]:
        rows = self.db.fetch_all(
            """
            SELECT w.id,w.path,u.username
            FROM workspaces w
            JOIN users u ON u.id=w.user_id
            WHERE w.problem_id=?
            ORDER BY u.username ASC
            """,
            [int(problem_id)],
        )
        items: list[dict[str, object]] = []
        for row in rows:
            items.append(
                {
                    "id": int(row["id"]),
                    "path": str(row["path"] or ""),
                    "username": str(row["username"] or ""),
                }
            )
        return items

    def delete_problem_metadata(self, problem_id: int) -> None:
        def _tx(conn: sqlite3.Connection) -> None:
            active = conn.execute(
                """
                SELECT kind,id,status
                FROM (
                    SELECT 'verification' AS kind,id,status
                    FROM verifications
                    WHERE problem_id=? AND status IN ('queued','running')
                    UNION ALL
                    SELECT 'export' AS kind,id,status
                    FROM export_jobs
                    WHERE problem_id=? AND status IN ('queued','running')
                    UNION ALL
                    SELECT 'package-build' AS kind,id,status
                    FROM problem_package_builds
                    WHERE problem_id=? AND status IN ('queued','running')
                )
                ORDER BY kind,id
                LIMIT 1
                """,
                [
                    int(problem_id),
                    int(problem_id),
                    int(problem_id),
                ],
            ).fetchone()
            if active is not None:
                kind = str(active["kind"] or "job")
                raise ValueError(
                    f"cannot delete problem while {kind} jobs are active"
                )
            conn.execute("DELETE FROM contest_problems WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM export_jobs WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM exports WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM problem_package_builds WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM problem_package_materializations WHERE problem_id=?", [int(problem_id)])
            conn.execute(
                "DELETE FROM verification_task_artifacts WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_selected_tests WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_source_paths WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_sanity_check_messages WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_sanity_checks WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_tests_meta WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute(
                """
                DELETE FROM verification_task_diagnostics
                WHERE task_id IN (
                    SELECT task.id
                    FROM verification_tasks task
                    JOIN verifications verification
                      ON verification.id=task.verification_id
                    WHERE verification.problem_id=?
                )
                """,
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM verification_tasks WHERE verification_id IN (SELECT id FROM verifications WHERE problem_id=?)",
                [int(problem_id)],
            )
            conn.execute("DELETE FROM verifications WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM workspaces WHERE problem_id=?", [int(problem_id)])
            conn.execute(
                "DELETE FROM agent_access_requests WHERE problem_id=?",
                [int(problem_id)],
            )
            conn.execute(
                "DELETE FROM agent_problem_grants WHERE problem_id=?",
                [int(problem_id)],
            )
            conn.execute("DELETE FROM repo_acl WHERE problem_id=?", [int(problem_id)])
            conn.execute("DELETE FROM problems WHERE id=?", [int(problem_id)])

        self.verification_task_store.run_problem_deletion(
            problem_id,
            delete_metadata=_tx,
        )
