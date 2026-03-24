from __future__ import annotations

import json
import sqlite3
from typing import TypedDict

from app.db import DB


class ContestContextRecord(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    created_at: str


class ContestMemberRecord(TypedDict):
    username: str
    role: str
    created_at: str


class ContestPropertyRecord(TypedDict):
    key: str
    value_json: str


class ContestProblemRecord(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    created_at: str
    problem_slug: str
    problem_name: str


class ContestAvailableProblemRecord(TypedDict):
    problem_id: int
    problem_slug: str
    problem_name: str
    role: str


class ContestProblemLookupRecord(TypedDict):
    id: int
    slug: str
    name: str


class ContestSelectedProblemRecord(TypedDict):
    problem_id: int
    idx: str
    problem_slug: str
    problem_name: str


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
                       COALESCE((SELECT MAX(pr.updated_at) FROM contest_properties pr WHERE pr.contest_id=c.id), ''),
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
            WHERE m.user_id=?
            ORDER BY last_updated_at DESC, c.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), int(user_id), max(1, int(limit))],
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
            conn.execute(
                "INSERT INTO contests(slug,title,owner_user_id,created_at) VALUES(?,?,?,?)",
                [slug, title, int(owner_user_id), created_at],
            )
            contest_row = conn.execute("SELECT id FROM contests WHERE slug=?", [slug]).fetchone()
            if contest_row is None:
                raise RuntimeError("failed to create contest")
            contest_id = int(contest_row["id"])
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

    def add_problem(self, contest_id: int, idx: str, problem_id: int, added_by_user_id: int, created_at: str) -> None:
        self.db.execute(
            """
            INSERT INTO contest_problems(contest_id,idx,problem_id,added_by_user_id,created_at)
            VALUES(?,?,?,?,?)
            """,
            [int(contest_id), idx, int(problem_id), int(added_by_user_id), created_at],
        )

    def contest_context_row(self, contest_slug: str) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            "SELECT id,slug,title,owner_user_id,created_at FROM contests WHERE slug=?",
            [contest_slug],
        )
        return None if row is None else dict(row)

    def contest_context_by_id(self, contest_id: int) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            "SELECT id,slug,title,owner_user_id,created_at FROM contests WHERE id=?",
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
            SELECT u.username,m.role,m.created_at
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
        row = self.db.fetch_one("SELECT id FROM users WHERE username=?", [username])
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
            WHERE m.contest_id=? AND u.username=?
            """,
            [int(contest_id), username],
        )
        return None if row is None else dict(row)

    def revoke_member(self, contest_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM contest_members WHERE contest_id=? AND user_id=?",
            [int(contest_id), int(user_id)],
        )

    def property_rows(self, contest_id: int) -> list[ContestPropertyRecord]:
        rows = self.db.fetch_all(
            "SELECT key,value_json FROM contest_properties WHERE contest_id=?",
            [int(contest_id)],
        )
        return [dict(row) for row in rows]

    def property_row(self, contest_id: int, key: str) -> ContestPropertyRecord | None:
        row = self.db.fetch_one(
            "SELECT key,value_json FROM contest_properties WHERE contest_id=? AND key=?",
            [int(contest_id), str(key).strip()],
        )
        return None if row is None else dict(row)

    def update_title(self, contest_id: int, title: str) -> None:
        self.db.execute("UPDATE contests SET title=? WHERE id=?", [title, int(contest_id)])

    def upsert_property(self, contest_id: int, actor_user_id: int, key: str, value: object, updated_at: str) -> None:
        self.db.execute(
            """
            INSERT INTO contest_properties(contest_id,key,value_json,updated_at,updated_by_user_id)
            VALUES(?,?,?,?,?)
            ON CONFLICT(contest_id,key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at,
                updated_by_user_id=excluded.updated_by_user_id
            """,
            [int(contest_id), key, json.dumps(value), updated_at, int(actor_user_id)],
        )

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

    def contest_problem_rows(self, contest_id: int) -> list[ContestProblemRecord]:
        rows = self.db.fetch_all(
            """
            SELECT cp.id AS contest_problem_id,cp.idx,cp.problem_id,cp.created_at,p.slug AS problem_slug,p.name AS problem_name
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=?
            ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
            """,
            [int(contest_id)],
        )
        return [dict(row) for row in rows]

    def available_problem_rows(self, contest_id: int, user_id: int, *, limit: int) -> list[ContestAvailableProblemRecord]:
        rows = self.db.fetch_all(
            """
            SELECT p.id AS problem_id,p.slug AS problem_slug,p.name AS problem_name,a.role
            FROM repo_acl a
            JOIN problems p ON p.id=a.problem_id
            WHERE a.user_id=?
              AND p.id NOT IN (
                  SELECT cp.problem_id
                  FROM contest_problems cp
                  WHERE cp.contest_id=?
              )
            ORDER BY p.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(contest_id), max(1, int(limit))],
        )
        return [dict(row) for row in rows]

    def problem_count(self, contest_id: int) -> int:
        row = self.db.fetch_one("SELECT COUNT(*) AS c FROM contest_problems WHERE contest_id=?", [int(contest_id)])
        if row is None:
            return 0
        return max(0, int(row["c"] or 0))

    def problem_by_slug(self, slug: str) -> ContestProblemLookupRecord | None:
        row = self.db.fetch_one("SELECT id,slug,name FROM problems WHERE slug=?", [slug])
        return None if row is None else dict(row)

    def contest_has_problem(self, contest_id: int, problem_id: int) -> bool:
        row = self.db.fetch_one(
            "SELECT 1 FROM contest_problems WHERE contest_id=? AND problem_id=? LIMIT 1",
            [int(contest_id), int(problem_id)],
        )
        return row is not None

    def used_problem_indices(self, contest_id: int) -> list[str]:
        rows = self.db.fetch_all("SELECT idx FROM contest_problems WHERE contest_id=?", [int(contest_id)])
        return [str(row["idx"]) for row in rows if str(row["idx"]).strip()]

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
        safe_pairs = [(int(problem_id), idx) for problem_id, idx in pairs if idx]
        if not safe_pairs:
            return False
        def tx(conn: sqlite3.Connection) -> int:
            updated = 0
            for problem_id, idx in safe_pairs:
                row = conn.execute(
                    "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
                    [int(contest_id), problem_id],
                ).fetchone()
                if row is None:
                    continue
                conn.execute(
                    "UPDATE contest_problems SET idx=? WHERE contest_id=? AND problem_id=?",
                    [idx, int(contest_id), problem_id],
                )
                updated += 1
            return updated
        return int(self.db.write_transaction(tx)) > 0

    def renumber_problem_indices(self, contest_id: int) -> None:
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
                "SELECT problem_id FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
                [int(contest_id)],
            ).fetchall()
            for pos, row in enumerate(rows, start=1):
                conn.execute(
                    "UPDATE contest_problems SET idx=? WHERE contest_id=? AND problem_id=?",
                    [idx_label(pos), int(contest_id), int(row["problem_id"])],
                )
            return len(rows)
        self.db.write_transaction(tx)

    def selected_problem_rows(self, contest_id: int, problem_ids: list[int]) -> list[ContestSelectedProblemRecord]:
        safe_problem_ids = [int(problem_id) for problem_id in problem_ids]
        if not safe_problem_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_problem_ids))
        rows = self.db.fetch_all(
            f"""
            SELECT cp.problem_id,cp.idx,p.slug AS problem_slug,p.name AS problem_name
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=? AND cp.problem_id IN ({placeholders})
            ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
            """,
            [int(contest_id), *safe_problem_ids],
        )
        return [dict(row) for row in rows]

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
            INSERT INTO contest_jobs(id,contest_id,actor_user_id,job_type,status,created_at,finished_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            [job_id, int(contest_id), int(actor_user_id), job_type, status, created_at, finished_at],
        )

    def update_job(
        self,
        *,
        contest_id: int,
        job_id: str,
        status: str,
        finished_at: str | None,
    ) -> None:
        self.db.execute(
            """
            UPDATE contest_jobs
            SET status=?, finished_at=?
            WHERE contest_id=? AND id=?
            """,
            [status, finished_at, int(contest_id), job_id],
        )

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

    def job_status(self, contest_id: int, job_id: str) -> str:
        row = self.db.fetch_one("SELECT status FROM contest_jobs WHERE contest_id=? AND id=?", [int(contest_id), job_id])
        return "" if row is None else str(row["status"] or "")

    def running_job_id(self, contest_id: int, job_type: str) -> str:
        row = self.db.fetch_one(
            """
            SELECT id
            FROM contest_jobs
            WHERE contest_id=? AND job_type=? AND status IN ('running','queued')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            [int(contest_id), job_type],
        )
        return "" if row is None else str(row["id"])

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
