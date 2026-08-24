from typing import TypedDict

import sqlite3

from app.db import DB
from app.main_util import problem_slug_leaf
from app.service.contest.model import (
    AgentContestRoster,
    AgentContestRosterProblem,
)
from app.service.contest.problem_index import contest_problem_idx_sort_key


class ContestContextRecord(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    status: str
    source_generation: int
    created_at: str


class ContestMemberRecord(TypedDict):
    user_id: int
    username: str
    role: str
    created_at: str
    is_system_admin: int


class ContestOverviewRecord(TypedDict):
    id: int
    slug: str
    title: str
    owner_user_id: int
    created_at: str
    role: str
    last_updated_at: str
    problem_count: int
    dirty_problem_count: int


class ContestMembershipRecord(TypedDict):
    user_id: int
    role: str


class ContestProblemRecord(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    statement_folder: str
    created_at: str
    problem_slug: str
    slug_leaf: str


class ContestProblemLookupRecord(TypedDict):
    id: int
    slug: str


class ContestSelectedProblemRecord(TypedDict):
    problem_id: int
    idx: str
    problem_slug: str
    slug_leaf: str


class ContestAttachmentRecord(TypedDict):
    key: str
    rel_path: str
    created_at: str


def _required_int(value: object, column: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"contest {column} must be an integer")
    return value


def _contest_context_record(row: dict[str, object]) -> ContestContextRecord:
    return {
        "id": _required_int(row["id"], "id"),
        "slug": str(row["slug"] or ""),
        "title": str(row["title"] or ""),
        "owner_user_id": _required_int(row["owner_user_id"], "owner_user_id"),
        "status": str(row["status"] or ""),
        "source_generation": _required_int(
            row["source_generation"], "source_generation"
        ),
        "created_at": str(row["created_at"] or ""),
    }


def _contest_member_record(row: dict[str, object]) -> ContestMemberRecord:
    return {
        "user_id": _required_int(row["user_id"], "member user_id"),
        "username": str(row["username"] or ""),
        "role": str(row["role"] or ""),
        "created_at": str(row["created_at"] or ""),
        "is_system_admin": _required_int(
            row["is_system_admin"], "member is_system_admin"
        ),
    }


def _contest_overview_record(row: dict[str, object]) -> ContestOverviewRecord:
    return {
        "id": _required_int(row["id"], "overview id"),
        "slug": str(row["slug"] or ""),
        "title": str(row["title"] or ""),
        "owner_user_id": _required_int(
            row["owner_user_id"], "overview owner_user_id"
        ),
        "created_at": str(row["created_at"] or ""),
        "role": str(row["role"] or ""),
        "last_updated_at": str(row["last_updated_at"] or ""),
        "problem_count": _required_int(row["problem_count"], "problem_count"),
        "dirty_problem_count": _required_int(
            row["dirty_problem_count"], "dirty_problem_count"
        ),
    }


def _contest_attachment_record(row: dict[str, object]) -> ContestAttachmentRecord:
    return {
        "key": str(row["key"] or ""),
        "rel_path": str(row["rel_path"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


class ContestDiskStore:
    def __init__(self, db: DB):
        self.db = db

    def user_contest_rows(self, user_id: int, *, limit: int) -> list[ContestOverviewRecord]:
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
        return [_contest_overview_record(dict(row)) for row in rows]

    def all_contest_rows(self, user_id: int, *, limit: int) -> list[ContestOverviewRecord]:
        rows = self.db.fetch_all(
            """
            SELECT c.id,c.slug,COALESCE(title_property.value, '') AS title,
                   c.owner_user_id,c.created_at,'admin' AS role,
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
                       SELECT COUNT(*)
                       FROM contest_problems cp3
                       JOIN workspaces w ON w.problem_id=cp3.problem_id AND w.user_id=?
                       WHERE cp3.contest_id=c.id
                         AND COALESCE(w.dirty, 0) <> 0
                   ) AS dirty_problem_count
            FROM contests c
            LEFT JOIN contest_properties title_property
              ON title_property.contest_id=c.id AND title_property.key='title'
            ORDER BY last_updated_at DESC, c.slug ASC
            LIMIT ?
            """,
            [int(user_id), int(user_id), max(1, int(limit))],
        )
        return [_contest_overview_record(dict(row)) for row in rows]

    def contest_slug_exists(self, contest_slug: str) -> bool:
        row = self.db.fetch_one("SELECT id FROM contests WHERE slug=?", [contest_slug])
        return row is not None

    def create_contest_with_owner(self, *, slug: str, title: str, owner_user_id: int, created_at: str) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            exists = conn.execute("SELECT id FROM contests WHERE slug=?", [slug]).fetchone()
            if exists is not None:
                raise ValueError("contest slug already exists")
            cursor = conn.execute(
                "INSERT INTO contests(slug,owner_user_id,created_at) VALUES(?,?,?)",
                [slug, int(owner_user_id), created_at],
            )
            if cursor.lastrowid is None:
                raise RuntimeError("contest insert did not return an id")
            contest_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO contest_members(contest_id,user_id,role,created_at) VALUES(?,?,?,?)",
                [contest_id, int(owner_user_id), "owner", created_at],
            )
            conn.execute(
                "INSERT INTO contest_properties(contest_id,key,value) VALUES(?, 'title', ?)",
                [contest_id, title],
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
    ) -> int:
        def tx(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                """
                SELECT COUNT(*) AS problem_count
                FROM contest_problems WHERE contest_id=?
                """,
                [int(contest_id)],
            ).fetchone()
            if int(row["problem_count"]) >= int(max_problems):
                raise ValueError(
                    f"contest already has the configured maximum of {int(max_problems)} problems"
                )
            cursor = conn.execute(
                """
                INSERT INTO contest_problems(
                    contest_id,idx,problem_id,statement_folder,added_by_user_id,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                [int(contest_id), idx, int(problem_id), "", int(added_by_user_id), created_at],
            )
            if cursor.lastrowid is None:
                raise RuntimeError("contest problem insert did not return an id")
            return int(cursor.lastrowid)
        return int(self.db.write_transaction(tx))

    def contest_context_row(self, contest_slug: str) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            """
            SELECT c.id,c.slug,COALESCE(title_property.value, '') AS title,
                   c.owner_user_id,c.status,c.source_generation,c.created_at
            FROM contests c
            LEFT JOIN contest_properties title_property
              ON title_property.contest_id=c.id AND title_property.key='title'
            WHERE c.slug=?
            """,
            [contest_slug],
        )
        return None if row is None else _contest_context_record(dict(row))

    def agent_roster(self, contest_slug: str) -> AgentContestRoster | None:
        with self.db.conn() as connection:
            connection.execute("BEGIN")
            contest = connection.execute(
                """
                SELECT c.id,c.slug,COALESCE(title_property.value, '') AS title,
                       c.source_generation
                FROM contests c
                LEFT JOIN contest_properties title_property
                  ON title_property.contest_id=c.id AND title_property.key='title'
                WHERE c.slug=?
                """,
                [contest_slug],
            ).fetchone()
            if contest is None:
                connection.rollback()
                return None
            rows = connection.execute(
                """
                SELECT cp.id AS contest_problem_id,cp.idx,
                       cp.problem_id,p.slug AS problem_slug
                FROM contest_problems cp
                JOIN problems p ON p.id=cp.problem_id
                WHERE cp.contest_id=?
                """,
                [int(contest["id"])],
            ).fetchall()
            connection.commit()
        problems: list[AgentContestRosterProblem] = [
            {
                "contest_problem_id": _required_int(
                    row["contest_problem_id"],
                    "agent roster contest_problem_id",
                ),
                "idx": str(row["idx"]),
                "problem_id": _required_int(
                    row["problem_id"],
                    "agent roster problem_id",
                ),
                "problem_slug": str(row["problem_slug"] or ""),
            }
            for row in sorted(
                rows,
                key=lambda item: contest_problem_idx_sort_key(str(item["idx"])),
            )
        ]
        return {
            "contest_id": _required_int(contest["id"], "agent roster id"),
            "contest_slug": str(contest["slug"] or ""),
            "contest_title": str(contest["title"] or ""),
            "source_generation": _required_int(
                contest["source_generation"],
                "agent roster source_generation",
            ),
            "problems": problems,
        }

    def contest_context_by_id(self, contest_id: int) -> ContestContextRecord | None:
        row = self.db.fetch_one(
            """
            SELECT c.id,c.slug,COALESCE(title_property.value, '') AS title,
                   c.owner_user_id,c.status,c.source_generation,c.created_at
            FROM contests c
            LEFT JOIN contest_properties title_property
              ON title_property.contest_id=c.id AND title_property.key='title'
            WHERE c.id=?
            """,
            [int(contest_id)],
        )
        return None if row is None else _contest_context_record(dict(row))

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
            SELECT u.id AS user_id,u.username,m.role,m.created_at,
                   COALESCE(u.is_system_admin, 0) AS is_system_admin
            FROM contest_members m
            JOIN users u ON u.id=m.user_id
            WHERE m.contest_id=?
            ORDER BY
                CASE m.role WHEN 'owner' THEN 0 WHEN 'write' THEN 1 ELSE 2 END,
                u.username ASC
            """,
            [int(contest_id)],
        )
        return [_contest_member_record(dict(row)) for row in rows]

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

    def membership_for_username(
        self, contest_id: int, username: str
    ) -> ContestMembershipRecord | None:
        row = self.db.fetch_one(
            """
            SELECT u.id AS user_id,m.role
            FROM contest_members m
            JOIN users u ON u.id=m.user_id
            WHERE m.contest_id=? AND LOWER(u.username)=LOWER(?)
            """,
            [int(contest_id), username],
        )
        if row is None:
            return None
        return {
            "user_id": _required_int(row["user_id"], "membership user_id"),
            "role": str(row["role"] or ""),
        }

    def revoke_member(self, contest_id: int, user_id: int) -> None:
        self.db.execute(
            "DELETE FROM contest_members WHERE contest_id=? AND user_id=?",
            [int(contest_id), int(user_id)],
        )

    def property_map(self, contest_id: int) -> dict[str, str]:
        rows = self.db.fetch_all(
            "SELECT key,value FROM contest_properties WHERE contest_id=? ORDER BY key",
            [int(contest_id)],
        )
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_property_values(
        self,
        contest_id: int,
        values: dict[str, str | None],
    ) -> bool:
        """Apply a property-map patch and bump generation once when it changes."""

        safe_contest_id = int(contest_id)
        safe_values = {
            str(key): None if value is None else str(value)
            for key, value in values.items()
        }

        def tx(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                "SELECT key,value FROM contest_properties WHERE contest_id=?",
                [safe_contest_id],
            ).fetchall()
            current = {str(row["key"]): str(row["value"]) for row in rows}
            changed = False
            for key, value in safe_values.items():
                if value is None:
                    if key not in current:
                        continue
                    conn.execute(
                        "DELETE FROM contest_properties WHERE contest_id=? AND key=?",
                        [safe_contest_id, key],
                    )
                    changed = True
                    continue
                if current.get(key) == value:
                    continue
                conn.execute(
                    """
                    INSERT INTO contest_properties(contest_id,key,value)
                    VALUES(?,?,?)
                    ON CONFLICT(contest_id,key) DO UPDATE SET value=excluded.value
                    """,
                    [safe_contest_id, key, value],
                )
                changed = True
            if changed:
                conn.execute(
                    "UPDATE contests SET source_generation=source_generation+1 WHERE id=?",
                    [safe_contest_id],
                )
            return 1 if changed else 0

        return bool(self.db.write_transaction(tx))

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
        return [_contest_attachment_record(dict(row)) for row in rows]

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

    def delete_attachment_rows_and_bump(
        self,
        contest_id: int,
        keys: list[str],
    ) -> int:
        safe_keys = sorted({str(key).strip() for key in keys if str(key).strip()})
        if not safe_keys:
            return 0
        placeholders = ",".join("?" for _ in safe_keys)

        def tx(conn: sqlite3.Connection) -> int:
            rows = conn.execute(
                f"SELECT key FROM contest_attachments "
                f"WHERE contest_id=? AND key IN ({placeholders})",
                [int(contest_id), *safe_keys],
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                f"DELETE FROM contest_attachments "
                f"WHERE contest_id=? AND key IN ({placeholders})",
                [int(contest_id), *safe_keys],
            )
            conn.execute(
                "UPDATE contests SET source_generation=source_generation+1 WHERE id=?",
                [int(contest_id)],
            )
            return len(rows)

        return int(self.db.write_transaction(tx))

    def contest_problem_rows(self, contest_id: int) -> list[ContestProblemRecord]:
        rows = self.db.fetch_all(
            """
            SELECT cp.id AS contest_problem_id,cp.idx,cp.problem_id,
                   cp.statement_folder,cp.created_at,p.slug AS problem_slug
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=?
            """,
            [int(contest_id)],
        )
        items: list[ContestProblemRecord] = []
        for row in sorted(
            rows,
            key=lambda item: contest_problem_idx_sort_key(str(item["idx"])),
        ):
            safe_slug = str(row["problem_slug"] or "")
            items.append(
                {
                    "contest_problem_id": int(row["contest_problem_id"]),
                    "idx": str(row["idx"]),
                    "problem_id": int(row["problem_id"]),
                    "statement_folder": str(row["statement_folder"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "problem_slug": safe_slug,
                    "slug_leaf": problem_slug_leaf(safe_slug),
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
        rows = self.db.fetch_all("SELECT idx FROM contest_problems WHERE contest_id=?", [int(contest_id)])
        return [str(row["idx"]) for row in rows]

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

    def set_problem_indices(self, contest_id: int, pairs: list[tuple[int, str]]) -> bool:
        safe_pairs = [(int(contest_problem_id), idx) for contest_problem_id, idx in pairs]
        if not safe_pairs:
            raise ValueError("problem indices must include every contest problem")
        def tx(conn: sqlite3.Connection) -> int:
            contest_problem_ids = [contest_problem_id for contest_problem_id, _ in safe_pairs]
            if len(set(contest_problem_ids)) != len(contest_problem_ids):
                raise ValueError("duplicate contest problem id")
            if len({idx for _, idx in safe_pairs}) != len(safe_pairs):
                raise ValueError("duplicate problem index")
            rows = conn.execute(
                "SELECT id,idx FROM contest_problems WHERE contest_id=?",
                [int(contest_id)],
            ).fetchall()
            found_ids = {int(row["id"]) for row in rows}
            if found_ids != set(contest_problem_ids):
                raise ValueError("problem indices must include every contest problem")
            current = {int(row["id"]): str(row["idx"]) for row in rows}
            requested = dict(safe_pairs)
            if current == requested:
                return 0

            for contest_problem_id, _ in safe_pairs:
                conn.execute(
                    "UPDATE contest_problems SET idx=? WHERE contest_id=? AND id=?",
                    [f"~TMP-{contest_problem_id}~", int(contest_id), contest_problem_id],
                )
            for contest_problem_id, idx in safe_pairs:
                conn.execute(
                    "UPDATE contest_problems SET idx=? WHERE contest_id=? AND id=?",
                    [idx, int(contest_id), contest_problem_id],
                )
            conn.execute(
                "UPDATE contests SET source_generation=source_generation+1 WHERE id=?",
                [int(contest_id)],
            )
            return len(safe_pairs)
        return int(self.db.write_transaction(tx)) > 0

    def delete_contest(self, contest_id: int) -> None:
        def tx(conn: sqlite3.Connection) -> None:
            safe_contest_id = int(contest_id)
            conn.execute("DELETE FROM contest_attachments WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_problems WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_members WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contest_properties WHERE contest_id=?", [safe_contest_id])
            conn.execute("DELETE FROM contests WHERE id=?", [safe_contest_id])

        self.db.write_transaction(tx)

    def selected_problem_rows(self, contest_id: int, problem_ids: list[int]) -> list[ContestSelectedProblemRecord]:
        safe_problem_ids = [int(problem_id) for problem_id in problem_ids]
        if not safe_problem_ids:
            return []
        placeholders = ",".join(("?" for _ in safe_problem_ids))
        rows = self.db.fetch_all(
            f"""
            SELECT cp.problem_id,cp.idx,p.slug AS problem_slug
            FROM contest_problems cp
            JOIN problems p ON p.id=cp.problem_id
            WHERE cp.contest_id=? AND cp.problem_id IN ({placeholders})
            """,
            [int(contest_id), *safe_problem_ids],
        )
        items: list[ContestSelectedProblemRecord] = []
        for row in sorted(
            rows,
            key=lambda item: contest_problem_idx_sort_key(str(item["idx"])),
        ):
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
