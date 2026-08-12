from __future__ import annotations

import sqlite3
from typing import TypedDict

from app.main_util import problem_slug_leaf
from app.db import DB, is_sqlite_locked_error
from app.service.contest.model import ContestBuildFreezeResult, ContestBuildRevision


class ContestContextRecord(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    status: str
    source_generation: int
    location: str
    date_text: str
    statement_default_language: str
    created_at: str


class ContestMemberRecord(TypedDict):
    username: str
    role: str
    created_at: str
    is_system_admin: int


class ContestProblemRecord(TypedDict):
    contest_problem_id: int
    position: int
    idx: str
    problem_id: int
    statement_folder: str
    created_at: str
    problem_slug: str
    slug_leaf: str


class ContestAvailableProblemRecord(TypedDict):
    problem_id: int
    problem_slug: str
    slug_leaf: str
    role: str


class ContestProblemLookupRecord(TypedDict):
    id: int
    slug: str


class ContestSelectedProblemRecord(TypedDict):
    problem_id: int
    idx: str
    problem_slug: str
    slug_leaf: str


class ContestJobRecord(TypedDict):
    id: str
    contest_slug: str
    job_type: str
    status: str
    created_at: str
    finished_at: str


class ContestArtifactRecord(TypedDict):
    id: str
    job_id: str
    artifact_type: str
    filename: str
    size_bytes: int
    created_at: str


class ContestAttachmentRecord(TypedDict):
    key: str
    rel_path: str
    created_at: str


class ContestDiskStore:
    def __init__(self, db: DB):
        self.db = db

    def user_contest_rows(self, user_id: int, *, limit: int) -> list[dict[str, object]]:
        rows = self.db.fetch_all(
            """
            SELECT c.id,c.slug,c.title,c.owner_user_id,c.created_at,m.role,
                   MAX(
                       c.created_at,
                       COALESCE((SELECT MAX(cp0.created_at) FROM contest_problems cp0 WHERE cp0.contest_id=c.id), ''),
                       COALESCE((SELECT MAX(cj.created_at) FROM contest_jobs cj WHERE cj.contest_id=c.id), ''),
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
                       SELECT COUNT(*)
                       FROM contest_problems cp3
                       JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?
                       WHERE cp3.contest_id=c.id
                         AND COALESCE(w.dirty, 0) <> 0
                   ) AS dirty_problem_count
            FROM contests c
            JOIN contest_members m ON m.contest_id=c.id
            WHERE m.user_id=?
            ORDER BY last_updated_at DESC, c.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), int(user_id), max(1, int(limit))],
        )
        return [dict(row) for row in rows]

    def all_contest_rows(self, user_id: int, *, limit: int) -> list[dict[str, object]]:
        rows = self.db.fetch_all(
            """
            SELECT c.id,c.slug,c.title,c.owner_user_id,c.created_at,'admin' AS role,
                   MAX(
                       c.created_at,
                       COALESCE((SELECT MAX(cp0.created_at) FROM contest_problems cp0 WHERE cp0.contest_id=c.id), ''),
                       COALESCE((SELECT MAX(cj.created_at) FROM contest_jobs cj WHERE cj.contest_id=c.id), ''),
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
                       SELECT COUNT(*)
                       FROM contest_problems cp3
                       JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?
                       WHERE cp3.contest_id=c.id
                         AND COALESCE(w.dirty, 0) <> 0
                   ) AS dirty_problem_count
            FROM contests c
            ORDER BY last_updated_at DESC, c.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), max(1, int(limit))],
        )
        return [dict(row) for row in rows]

    def contest_slug_exists(self, contest_slug: str) -> bool:
        row = self.db.fetch_one("SELECT id FROM contests WHERE slug=?", [contest_slug])
        return row is not None

    def create_contest_with_owner(self, *, slug: str, title: str, owner_user_id: int, created_at: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            exists = conn.execute("SELECT id FROM contests WHERE slug=?", [slug]).fetchone()
            if exists is not None:
                raise ValueError("contest slug already exists")
            cursor = conn.execute(
                "INSERT INTO contests(slug,title,owner_user_id,created_at) VALUES(?,?,?,?)",
                [slug, title, int(owner_user_id), created_at],
            )
            contest_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
                [contest_id, int(owner_user_id), "owner", created_at],
            )
            return contest_id

        try:
            return int(self.db.write_transaction(tx))
        except sqlite3.IntegrityError as exc:
            message = str(exc).strip().lower()
            if "contests.slug" in message:
                raise ValueError("contest slug already exists") from exc
            raise

    def add_problem(
        self,
        contest_id: int,
        idx: str,
        problem_id: int,
        added_by_user_id: int,
        created_at: str,
        *,
        max_problems: int,
    ) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                """
                SELECT COUNT(*) AS problem_count,
                       COALESCE(MAX(position),0)+1 AS next_position
                FROM contest_problems WHERE contest_id=?
                """,
                [int(contest_id)],
            ).fetchone()
            if int(row["problem_count"]) >= int(max_problems):
                raise ValueError(
                    f"contest already has the configured maximum of {int(max_problems)} problems"
                )
            position = int(row["next_position"])
            conn.execute(
                """
                INSERT INTO contest_problems(
                    contest_id,position,label,problem_id,statement_folder,added_by_user_id,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [int(contest_id), position, idx, int(problem_id), "", int(added_by_user_id), created_at],
            )
        self.db.write_transaction(tx)

    def contest_context_row(self, contest_slug: str) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            """
            SELECT id,slug,title,owner_user_id,status,source_generation,location,date_text,
                   statement_default_language,created_at
            FROM contests WHERE slug=?
            """,
            [contest_slug],
        )
        return None if row is None else dict(row)

    def contest_context_by_id(self, contest_id: int) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            """
            SELECT id,slug,title,owner_user_id,status,source_generation,location,date_text,
                   statement_default_language,created_at
            FROM contests WHERE id=?
            """,
            [int(contest_id)],
        )
        return None if row is None else dict(row)

    def contest_role(self, contest_id: int, user_id: int) -> str | None:
        row = self.db.fetch_one(
            "SELECT role FROM contest_members WHERE contest_id=? AND user_id=?",
            [int(contest_id), int(user_id)],
        )
        if row is None:
            return None
        return str(row["role"])

    def owner_count(self, contest_id: int) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM contest_members WHERE contest_id=? AND role='owner'",
            [int(contest_id)],
        )
        if row is None:
            return 0
        return max(0, int(row["c"] or 0))

    def member_count(self, contest_id: int) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS c FROM contest_members WHERE contest_id=?",
            [int(contest_id)],
        )
        if row is None:
            return 0
        return max(0, int(row["c"] or 0))

    def member_entries(self, contest_id: int) -> list[ContestMemberRecord]:
        rows = self.db.fetch_all(
            """
            SELECT u.username,m.role,m.created_at,COALESCE(u.is_system_admin, 0) AS is_system_admin
            FROM contest_members m
            JOIN users u ON u.id=m.user_id
            WHERE m.contest_id=?
            ORDER BY
                CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
                u.username ASC
            """,
            [int(contest_id)],
        )
        return [dict(row) for row in rows]

    def user_id_by_username(self, username: str) -> int | None:
        row = self.db.fetch_one(
            "SELECT id FROM users WHERE LOWER(username)=LOWER(?) ORDER BY id ASC LIMIT 1",
            [username],
        )
        if row is None:
            return None
        return int(row["id"])

    def grant_member_role(self, contest_id: int, user_id: int, role: str, created_at: str) -> None:
        self.db.execute(
            """
            INSERT INTO contest_members(contest_id,user_id,role,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(contest_id,user_id) DO UPDATE SET role=excluded.role
            """,
            [int(contest_id), int(user_id), role, created_at],
        )

    def membership_for_username(self, contest_id: int, username: str) -> dict[str, object] | None:
        row = self.db.fetch_one(
            """
            SELECT u.id AS user_id,m.role
            FROM contest_members m
            JOIN users u ON u.id=m.user_id
            WHERE m.contest_id=? AND LOWER(u.username)=LOWER(?)
            """,
            [int(contest_id), username],
        )
        return None if row is None else dict(row)

    def is_system_admin(self, user_id: int) -> bool:
        row = self.db.fetch_one("SELECT is_system_admin FROM users WHERE id=?", [int(user_id)])
        if row is None:
            return False
        return int(row["is_system_admin"] or 0) == 1

    def revoke_member(self, contest_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM contest_members WHERE contest_id=? AND user_id=?",
            [int(contest_id), int(user_id)],
        )

    def update_title(self, contest_id: int, title: str) -> None:
        self.db.execute("UPDATE contests SET title=? WHERE id=?", [title, int(contest_id)])

    def update_metadata_field(self, contest_id: int, column: str, value: str) -> None:
        columns = {
            "location": "location",
            "date": "date_text",
            "statement_default_language": "statement_default_language",
        }
        target = columns[column]
        self.db.execute(
            f"UPDATE contests SET {target}=? WHERE id=?",
            [value, int(contest_id)],
        )

    def set_problem_statement_folders(self, contest_id: int, source_folders: dict[int, str]) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE contest_problems SET statement_folder='' WHERE contest_id=?", [int(contest_id)])
            for problem_id, folder in source_folders.items():
                conn.execute(
                    "UPDATE contest_problems SET statement_folder=? WHERE contest_id=? AND problem_id=?",
                    [folder, int(contest_id), int(problem_id)],
                )
        self.db.write_transaction(tx)

    def attachment_rows(self, contest_id: int) -> list[ContestAttachmentRecord]:
        rows = self.db.fetch_all(
            """
            SELECT key,rel_path,created_at
            FROM contest_attachments
            WHERE contest_id=?
            ORDER BY key ASC
            """,
            [int(contest_id)],
        )
        return [dict(row) for row in rows]

    def replace_attachment_rows(
        self,
        *,
        contest_id: int,
        created_by_user_id: int,
        created_at: str,
        rows: list[tuple[str, str]],
    ) -> None:
        safe_rows = [(str(key).strip(), str(rel_path).strip()) for key, rel_path in rows]

        def tx(conn: sqlite3.Connection) -> int:
            conn.execute("DELETE FROM contest_attachments WHERE contest_id=?", [int(contest_id)])
            for key, rel_path in safe_rows:
                conn.execute(
                    """
                    INSERT INTO contest_attachments(contest_id,key,rel_path,created_at,created_by_user_id)
                    VALUES(?,?,?,?,?)
                    """,
                    [int(contest_id), key, rel_path, created_at, int(created_by_user_id)],
                )
            return len(safe_rows)

        self.db.write_transaction(tx)

    def upsert_attachment_row(
        self,
        *,
        contest_id: int,
        key: str,
        rel_path: str,
        created_by_user_id: int,
        created_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO contest_attachments(contest_id,key,rel_path,created_at,created_by_user_id)
            VALUES(?,?,?,?,?)
            ON CONFLICT(contest_id,key) DO UPDATE SET
                rel_path=excluded.rel_path,
                created_at=excluded.created_at,
                created_by_user_id=excluded.created_by_user_id
            """,
            [int(contest_id), str(key).strip(), str(rel_path).strip(), created_at, int(created_by_user_id)],
        )

    def delete_attachment_row(self, contest_id: int, key: str) -> None:
        self.db.execute(
            "DELETE FROM contest_attachments WHERE contest_id=? AND key=?",
            [int(contest_id), str(key).strip()],
        )

    def contest_problem_rows(self, contest_id: int) -> list[ContestProblemRecord]:
        rows = self.db.fetch_all(
            """
            SELECT cp.id AS contest_problem_id,cp.position,cp.label AS idx,cp.problem_id,
                   cp.statement_folder,cp.created_at,p.slug AS problem_slug
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=?
            ORDER BY cp.position ASC, cp.id ASC
            """,
            [int(contest_id)],
        )
        items: list[ContestProblemRecord] = []
        for row in rows:
            safe_slug = str(row["problem_slug"] or "")
            items.append(
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "position": int(row["position"]),
                    "idx": str(row["idx"] or ""),
                    "problem_id": int(row["problem_id"]),
                    "statement_folder": str(row["statement_folder"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "problem_slug": safe_slug,
                    "slug_leaf": problem_slug_leaf(safe_slug),
                }
            )
        return items

    def available_problem_rows(self, contest_id: int, user_id: int, *, limit: int) -> list[ContestAvailableProblemRecord]:
        safe_contest_id = int(contest_id)
        safe_user_id = int(user_id)
        safe_limit = max(1, int(limit))
        if self.is_system_admin(safe_user_id):
            rows = self.db.fetch_all(
                """
                SELECT p.id AS problem_id,p.slug AS problem_slug,'admin' AS role
                FROM problems p
                WHERE p.id NOT IN (
                    SELECT cp.problem_id
                    FROM contest_problems cp
                    WHERE cp.contest_id=?
                )
                ORDER BY p.slug ASC
                LIMIT ?
                """,
                [safe_contest_id, safe_limit],
            )
        else:
            rows = self.db.fetch_all(
                """
                SELECT p.id AS problem_id,p.slug AS problem_slug,a.role
                FROM repo_acl a
                JOIN problems p ON p.id=a.problem_id
                WHERE a.user_id=?
                  AND a.role IN ('owner','admin')
                  AND p.id NOT IN (
                      SELECT cp.problem_id
                      FROM contest_problems cp
                      WHERE cp.contest_id=?
                  )
                ORDER BY p.slug ASC
                LIMIT ?
                """,
                [safe_user_id, safe_contest_id, safe_limit],
            )
        items: list[ContestAvailableProblemRecord] = []
        for row in rows:
            safe_slug = str(row["problem_slug"] or "")
            items.append(
                {
                    "problem_id": int(row["problem_id"]),
                    "problem_slug": safe_slug,
                    "slug_leaf": problem_slug_leaf(safe_slug),
                    "role": str(row["role"] or ""),
                }
            )
        return items

    def problem_count(self, contest_id: int) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [int(contest_id)])
        if row is None:
            return 0
        return max(0, int(row["c"] or 0))

    def problem_by_slug(self, slug: str) -> ContestProblemLookupRecord | None:
        row = self.db.fetch_one("SELECT id,slug FROM problems WHERE slug=?", [slug])
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"] or ""),
        }

    def contest_has_problem(self, contest_id: int, problem_id: int) -> bool:
        row = self.db.fetch_one(
            "SELECT 1 FROM contest_problems WHERE contest_id=? AND problem_id=? LIMIT 1",
            [int(contest_id), int(problem_id)],
        )
        return row is not None

    def used_problem_indices(self, contest_id: int) -> list[str]:
        rows = self.db.fetch_all("SELECT label FROM contest_problems WHERE contest_id=?", [int(contest_id)])
        return [str(row["label"]) for row in rows if str(row["label"]).strip()]

    def remove_problem(self, contest_id: int, problem_id: int) -> bool:
        return self.remove_problems(int(contest_id), [int(problem_id)]) > 0

    def remove_problems(self, contest_id: int, problem_ids: list[int]) -> int:
        safe_problem_ids = [int(problem_id) for problem_id in problem_ids]
        if not safe_problem_ids:
            return 0
        placeholders = ",".join(("?" for _ in safe_problem_ids))
        def tx(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"SELECT id FROM contest_problems WHERE contest_id=? AND problem_id IN ({placeholders})",
                [int(contest_id), *safe_problem_ids],
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                f"DELETE FROM contest_problems WHERE contest_id=? AND problem_id IN ({placeholders})",
                [int(contest_id), *safe_problem_ids],
            )
            return len(rows)
        return int(self.db.write_transaction(tx))

    def reorder_problem_indices(self, contest_id: int, pairs: list[tuple[int, str]]) -> bool:
        safe_pairs = [(int(contest_problem_id), idx) for contest_problem_id, idx in pairs if idx]
        if not safe_pairs:
            return False
        def tx(conn: sqlite3.Connection) -> int:
            contest_problem_ids = [contest_problem_id for contest_problem_id, _ in safe_pairs]
            if len(set(contest_problem_ids)) != len(contest_problem_ids):
                return 0
            if len({idx for _, idx in safe_pairs}) != len(safe_pairs):
                return 0
            rows = conn.execute(
                "SELECT id FROM contest_problems WHERE contest_id=?",
                [int(contest_id)],
            ).fetchall()
            found_ids = {int(row["id"]) for row in rows}
            if found_ids != set(contest_problem_ids):
                return 0

            for pos, (contest_problem_id, _) in enumerate(safe_pairs, start=1):
                conn.execute(
                    "UPDATE contest_problems SET position=?,label=? WHERE contest_id=? AND id=?",
                    [-pos, f"~tmp-reorder-{contest_problem_id}~", int(contest_id), contest_problem_id],
                )
            for pos, (contest_problem_id, idx) in enumerate(safe_pairs, start=1):
                conn.execute(
                    "UPDATE contest_problems SET position=?,label=? WHERE contest_id=? AND id=?",
                    [pos, idx, int(contest_id), contest_problem_id],
                )
            return len(safe_pairs)
        return int(self.db.write_transaction(tx)) > 0

    def renumber_problem_indices(
        self,
        contest_id: int,
        ordered_contest_problem_ids: list[int],
    ) -> bool:
        safe_ids = [int(contest_problem_id) for contest_problem_id in ordered_contest_problem_ids]
        if not safe_ids or len(set(safe_ids)) != len(safe_ids):
            return False

        def idx_label(seq: int) -> str:
            value = max(1, int(seq))
            chars: list[str] = []
            while value > 0:
                value -= 1
                chars.append(chr(ord("A") + (value % 26)))
                value //= 26
            return "".join(reversed(chars))

        def tx(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                "SELECT id FROM contest_problems WHERE contest_id=?",
                [int(contest_id)],
            ).fetchall()
            if {int(row["id"]) for row in rows} != set(safe_ids):
                return 0
            for pos, contest_problem_id in enumerate(safe_ids, start=1):
                conn.execute(
                    "UPDATE contest_problems SET position=?,label=? WHERE contest_id=? AND id=?",
                    [
                        -pos,
                        f"~tmp-renumber-{int(contest_id)}-{contest_problem_id}-{pos}~",
                        int(contest_id),
                        contest_problem_id,
                    ],
                )
            for pos, contest_problem_id in enumerate(safe_ids, start=1):
                conn.execute(
                    "UPDATE contest_problems SET position=?,label=? WHERE contest_id=? AND id=?",
                    [pos, idx_label(pos), int(contest_id), contest_problem_id],
                )
            return len(safe_ids)
        return int(self.db.write_transaction(tx)) == len(safe_ids)

    def delete_contest(self, contest_id: int) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            safe_contest_id = int(contest_id)
            conn.execute(
                "DELETE FROM contest_build_items WHERE job_id IN (SELECT id FROM contest_jobs WHERE contest_id=?)",
                [safe_contest_id],
            )
            conn.execute("DELETE FROM contest_artifacts WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_jobs WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_attachments WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_problems WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_members WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contests WHERE id=?", [safe_contest_id])

        self.db.write_transaction(tx)

    def selected_problem_rows(self, contest_id: int, problem_ids: list[int]) -> list[ContestSelectedProblemRecord]:
        safe_problem_ids = [int(problem_id) for problem_id in problem_ids]
        if not safe_problem_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_problem_ids))
        rows = self.db.fetch_all(
            f"""
            SELECT cp.problem_id,cp.label AS idx,p.slug AS problem_slug
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=? AND cp.problem_id IN ({placeholders})
            ORDER BY cp.position ASC, cp.id ASC
            """,
            [int(contest_id), *safe_problem_ids],
        )
        items: list[ContestSelectedProblemRecord] = []
        for row in rows:
            safe_slug = str(row["problem_slug"] or "")
            items.append(
                {
                    "problem_id": int(row["problem_id"]),
                    "idx": str(row["idx"] or ""),
                    "problem_slug": safe_slug,
                    "slug_leaf": problem_slug_leaf(safe_slug),
                }
            )
        return items

    def insert_job(
        self,
        *,
        job_id: str,
        contest_id: int,
        actor_user_id: int,
        job_type: str,
        status: str,
        created_at: str,
        finished_at: str | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO contest_jobs(
                id,contest_id,actor_user_id,job_type,status,source_generation,created_at,finished_at
            )
            VALUES(?,?,?,?,?,(SELECT source_generation FROM contests WHERE id=?),?,?)
            """,
            [
                job_id, int(contest_id), int(actor_user_id), job_type, status,
                int(contest_id), created_at, finished_at,
            ],
        )

    def freeze_build_job(
        self,
        *,
        job_id: str,
        contest_id: int,
        actor_user_id: int,
        job_type: str,
        created_at: str,
        revisions: list[ContestBuildRevision],
    ) -> ContestBuildFreezeResult:
        def transaction(connection) -> ContestBuildFreezeResult:
            contest = connection.execute(
                "SELECT slug,source_generation FROM contests WHERE id=?",
                [int(contest_id)],
            ).fetchone()
            if contest is None:
                raise ValueError("contest not found")
            active = connection.execute(
                """SELECT id FROM contest_jobs
                   WHERE contest_id=? AND job_type=? AND status IN ('running','queued')
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                [int(contest_id), job_type],
            ).fetchone()
            if active is not None:
                return {
                    "outcome": "already_running",
                    "job_id": str(active["id"]),
                    "contest_slug": str(contest["slug"]),
                    "blocked_problems": [],
                }
            rows = connection.execute(
                """SELECT cp.id AS contest_problem_id,cp.position,cp.label,
                       cp.problem_id,cp.statement_folder,p.slug AS problem_slug
                   FROM contest_problems cp
                   JOIN problems p ON p.id=cp.problem_id
                   WHERE cp.contest_id=?
                   ORDER BY cp.position,cp.id""",
                [int(contest_id)],
            ).fetchall()
            roster = [
                (
                    int(row["contest_problem_id"]),
                    int(row["position"]),
                    str(row["label"]),
                    int(row["problem_id"]),
                    str(row["statement_folder"]),
                    str(row["problem_slug"]),
                )
                for row in rows
            ]
            expected_roster = [
                (
                    int(row["contest_problem_id"]),
                    int(row["position"]),
                    str(row["label"]),
                    int(row["problem_id"]),
                    str(row["statement_folder"]),
                    str(row["problem_slug"]),
                )
                for row in revisions
            ]
            if roster != expected_roster:
                return {
                    "outcome": "roster_changed",
                    "job_id": "",
                    "contest_slug": str(contest["slug"]),
                    "blocked_problems": [],
                }
            blocked: list[str] = []
            materializations: dict[int, tuple[str | None, str | None]] = {}
            for revision in revisions:
                materialization = connection.execute(
                    """SELECT id,status,archive_sha256
                       FROM problem_package_materializations
                       WHERE problem_id=? AND source_commit=?""",
                    [int(revision["problem_id"]), str(revision["source_commit"])],
                ).fetchone()
                if materialization is not None and str(materialization["status"]) == "unavailable":
                    blocked.append(str(revision["problem_slug"]))
                    continue
                materializations[int(revision["contest_problem_id"])] = (
                    None if materialization is None else str(materialization["id"]),
                    None if materialization is None else str(materialization["archive_sha256"]),
                )
            if blocked:
                return {
                    "outcome": "not_ready",
                    "job_id": "",
                    "contest_slug": str(contest["slug"]),
                    "blocked_problems": blocked,
                }
            connection.execute(
                """INSERT INTO contest_jobs(
                       id,contest_id,actor_user_id,job_type,status,
                       source_generation,created_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                [
                    job_id,
                    int(contest_id),
                    int(actor_user_id),
                    job_type,
                    "queued",
                    int(contest["source_generation"]),
                    created_at,
                    None,
                ],
            )
            for revision in revisions:
                materialization_id, archive_sha256 = materializations[
                    int(revision["contest_problem_id"])
                ]
                connection.execute(
                    """INSERT INTO contest_build_items(
                           job_id,contest_problem_id,position,label,problem_id,
                           statement_folder,source_commit,revision_number,
                           materialization_id,archive_sha256
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    [
                        job_id,
                        int(revision["contest_problem_id"]),
                        int(revision["position"]),
                        str(revision["label"]),
                        int(revision["problem_id"]),
                        str(revision["statement_folder"]),
                        str(revision["source_commit"]),
                        int(revision["revision_number"]),
                        materialization_id,
                        archive_sha256,
                    ],
                )
            return {
                "outcome": "created",
                "job_id": job_id,
                "contest_slug": str(contest["slug"]),
                "blocked_problems": [],
            }

        with self.db.conn() as connection:
            connection.execute("PRAGMA busy_timeout=0")
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if not is_sqlite_locked_error(exc):
                    raise
                return {
                    "outcome": "busy",
                    "job_id": "",
                    "contest_slug": "",
                    "blocked_problems": [],
                }
            try:
                result = transaction(connection)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def bump_source_generation(self, contest_id: int) -> int:
        def transaction(connection) -> int:
            connection.execute(
                "UPDATE contests SET source_generation=source_generation+1 WHERE id=?",
                [int(contest_id)],
            )
            row = connection.execute(
                "SELECT source_generation FROM contests WHERE id=?",
                [int(contest_id)],
            ).fetchone()
            if row is None:
                raise ValueError("contest not found")
            return int(row["source_generation"])
        return self.db.write_transaction(transaction)

    def update_job(
        self,
        *,
        contest_id: int,
        job_id: str,
        status: str,
        finished_at: str | None,
    ) -> bool:
        def transaction(connection) -> bool:
            allowed = ("queued",) if status == "running" else ("queued", "running")
            cursor = connection.execute(
                """UPDATE contest_jobs SET status=?, finished_at=?
                   WHERE contest_id=? AND id=? AND status IN (?,?)""",
                [
                    status, finished_at, int(contest_id), job_id,
                    allowed[0], allowed[-1],
                ],
            )
            return int(cursor.rowcount or 0) == 1
        return self.db.write_transaction(transaction)

    def job_row(self, contest_id: int, job_id: str) -> ContestJobRecord | None:
        row = self.db.fetch_one(
            """
            SELECT cj.id,c.slug AS contest_slug,cj.job_type,cj.status,cj.created_at,cj.finished_at
            FROM contest_jobs cj
            JOIN contests c ON c.id=cj.contest_id
            WHERE cj.contest_id=? AND cj.id=?
            """,
            [int(contest_id), job_id],
        )
        return None if row is None else dict(row)

    def latest_job_row(self, contest_id: int) -> ContestJobRecord | None:
        row = self.db.fetch_one(
            """
            SELECT cj.id,c.slug AS contest_slug,cj.job_type,cj.status,cj.created_at,cj.finished_at
            FROM contest_jobs cj
            JOIN contests c ON c.id=cj.contest_id
            WHERE cj.contest_id=?
            ORDER BY cj.created_at DESC, cj.id DESC
            LIMIT 1
            """,
            [int(contest_id)],
        )
        return None if row is None else dict(row)

    def job_rows(self, contest_id: int, *, limit: int) -> list[ContestJobRecord]:
        rows = self.db.fetch_all(
            """
            SELECT cj.id,c.slug AS contest_slug,cj.job_type,cj.status,cj.created_at,cj.finished_at
            FROM contest_jobs cj
            JOIN contests c ON c.id=cj.contest_id
            WHERE cj.contest_id=?
            ORDER BY cj.created_at DESC, cj.id DESC
            LIMIT ?
            """,
            [int(contest_id), max(1, int(limit))],
        )
        return [dict(row) for row in rows]

    def insert_artifact(
        self,
        *,
        artifact_id: str,
        contest_id: int,
        job_id: str,
        artifact_type: str,
        filename: str,
        sha256: str,
        size_bytes: int,
        created_at: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO contest_artifacts(id,contest_id,job_id,artifact_type,filename,sha256,size_bytes,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            [artifact_id, int(contest_id), job_id, artifact_type, filename, sha256, int(size_bytes), created_at],
        )

    def build_items(self, job_id: str) -> list[dict[str, object]]:
        rows = self.db.fetch_all(
            """SELECT i.*,p.slug AS problem_slug
               FROM contest_build_items i JOIN problems p ON p.id=i.problem_id
               WHERE i.job_id=? ORDER BY i.position,i.id""",
            [job_id],
        )
        return [
            {
                "contest_problem_id": int(row["contest_problem_id"]),
                "position": int(row["position"]),
                "label": str(row["label"]),
                "idx": str(row["label"]),
                "problem_id": int(row["problem_id"]),
                "problem_slug": str(row["problem_slug"]),
                "statement_folder": str(row["statement_folder"]),
                "source_commit": str(row["source_commit"]),
                "revision_number": int(row["revision_number"]),
                "materialization_id": str(row["materialization_id"] or ""),
                "archive_sha256": str(row["archive_sha256"] or ""),
            }
            for row in rows
        ]

    def bind_build_item_materialization(
        self,
        *,
        job_id: str,
        contest_problem_id: int,
        problem_id: int,
        source_commit: str,
        materialization_id: str,
        archive_sha256: str,
    ) -> None:
        def transaction(connection) -> None:
            materialization = connection.execute(
                """SELECT problem_id,source_commit,archive_sha256,status
                   FROM problem_package_materializations WHERE id=?""",
                [materialization_id],
            ).fetchone()
            if materialization is None or str(materialization["status"]) != "available":
                raise ValueError("Native materialization is unavailable")
            if (
                int(materialization["problem_id"]) != int(problem_id)
                or str(materialization["source_commit"]) != source_commit
                or str(materialization["archive_sha256"]) != archive_sha256
            ):
                raise ValueError("Native materialization does not match frozen revision")
            cursor = connection.execute(
                """UPDATE contest_build_items
                   SET materialization_id=?,archive_sha256=?
                   WHERE job_id=? AND contest_problem_id=?
                     AND problem_id=? AND source_commit=?
                     AND (
                         (materialization_id IS NULL AND archive_sha256 IS NULL)
                         OR (materialization_id=? AND archive_sha256=?)
                     )""",
                [
                    materialization_id,
                    archive_sha256,
                    job_id,
                    int(contest_problem_id),
                    int(problem_id),
                    source_commit,
                    materialization_id,
                    archive_sha256,
                ],
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Contest build item Native identity no longer matches its frozen value"
                )

        self.db.write_transaction(transaction)

    def artifact_rows(self, contest_id: int, *, limit: int) -> list[ContestArtifactRecord]:
        rows = self.db.fetch_all(
            """
            SELECT id,job_id,artifact_type,filename,size_bytes,created_at
            FROM contest_artifacts
            WHERE contest_id=?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [int(contest_id), max(1, int(limit))],
        )
        return [dict(row) for row in rows]

    def artifact_row(self, contest_id: int, artifact_id: str) -> ContestArtifactRecord | None:
        row = self.db.fetch_one(
            """
            SELECT id,job_id,artifact_type,filename,size_bytes,created_at
            FROM contest_artifacts
            WHERE contest_id=? AND id=?
            """,
            [int(contest_id), artifact_id],
        )
        return None if row is None else dict(row)
