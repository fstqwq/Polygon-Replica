"""SQLite persistence for Native materializations and builds."""

from __future__ import annotations

from typing import TypedDict

from app.db import DB, now_iso


class PublishedProblem(TypedDict):
    id: int
    slug: str
    repo_name: str


class MaterializationRow(TypedDict):
    id: str
    problem_id: int
    source_commit: str
    revision_number: int
    source_digest: str
    archive_rel_path: str
    archive_sha256: str
    archive_size_bytes: int
    verification_id: str
    status: str
    created_at: str
    checked_at: str
    unavailable_reason: str


class BuildRow(TypedDict):
    id: str
    problem_id: int
    source_commit: str
    verification_id: str
    phase: str
    status: str
    materialization_id: str
    error: str


class MaterializationExportRow(TypedDict):
    export_type: str
    archive_rel_path: str


def _materialization(row) -> MaterializationRow:
    return {
        "id": str(row["id"]),
        "problem_id": int(row["problem_id"]),
        "source_commit": str(row["source_commit"]),
        "revision_number": int(row["revision_number"]),
        "source_digest": str(row["source_digest"]),
        "archive_rel_path": str(row["archive_rel_path"]),
        "archive_sha256": str(row["archive_sha256"]),
        "archive_size_bytes": int(row["archive_size_bytes"]),
        "verification_id": str(row["verification_id"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "checked_at": str(row["checked_at"]),
        "unavailable_reason": str(row["unavailable_reason"]),
    }


class ProblemPackageStore:
    def __init__(self, db: DB) -> None:
        self.db = db

    def problem(self, problem_id: int) -> PublishedProblem | None:
        row = self.db.fetch_one("SELECT id,slug,repo_name FROM problems WHERE id=?", [int(problem_id)])
        if row is None:
            return None
        return {"id": int(row["id"]), "slug": str(row["slug"]), "repo_name": str(row["repo_name"])}

    def materialization(self, materialization_id: str) -> MaterializationRow | None:
        row = self.db.fetch_one("SELECT * FROM problem_package_materializations WHERE id=?", [materialization_id])
        return None if row is None else _materialization(row)

    def materialization_for_revision(
        self, problem_id: int, source_commit: str
    ) -> MaterializationRow | None:
        row = self.db.fetch_one(
            """
            SELECT * FROM problem_package_materializations
            WHERE problem_id=? AND source_commit=?
            """,
            [int(problem_id), source_commit],
        )
        return None if row is None else _materialization(row)

    def all_available_materializations(self) -> list[MaterializationRow]:
        rows = self.db.fetch_all(
            """SELECT * FROM problem_package_materializations
               WHERE status='available' ORDER BY problem_id,revision_number"""
        )
        return [_materialization(row) for row in rows]

    def create_or_retry_build(
        self,
        *,
        build_id: str,
        problem_id: int,
        source_commit: str,
        verification_id: str,
    ) -> BuildRow:
        now = now_iso()

        def transaction(connection) -> BuildRow:
            row = connection.execute(
                """SELECT * FROM problem_package_builds
                   WHERE problem_id=? AND source_commit=?""",
                [problem_id, source_commit],
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO problem_package_builds(
                       id,problem_id,source_commit,verification_id,
                       phase,status,materialization_id,error,created_at,started_at,finished_at
                       ) VALUES(?,?,?,?,'queued','queued',NULL,'',?,NULL,NULL)""",
                    [build_id, problem_id, source_commit, verification_id, now],
                )
                row = connection.execute("SELECT * FROM problem_package_builds WHERE id=?", [build_id]).fetchone()
            elif str(row["status"]) in {"failed", "succeeded"}:
                connection.execute(
                    """UPDATE problem_package_builds SET verification_id=?,phase='queued',status='queued',
                       materialization_id=NULL,error='',started_at=NULL,finished_at=NULL WHERE id=?""",
                    [verification_id, str(row["id"])],
                )
                row = connection.execute(
                    "SELECT * FROM problem_package_builds WHERE id=?",
                    [str(row["id"])],
                ).fetchone()
            assert row is not None
            return {
                "id": str(row["id"]), "problem_id": int(row["problem_id"]),
                "source_commit": str(row["source_commit"]),
                "verification_id": str(row["verification_id"]), "phase": str(row["phase"]),
                "status": str(row["status"]), "materialization_id": str(row["materialization_id"] or ""),
                "error": str(row["error"]),
            }

        return self.db.write_transaction(transaction)

    def mark_build_running(self, build_id: str, *, phase: str) -> None:
        self.db.execute(
            """UPDATE problem_package_builds
               SET status='running',phase=?,started_at=COALESCE(started_at,?)
               WHERE id=? AND status IN ('queued','running')""",
            [phase, now_iso(), build_id],
        )

    def mark_build_phase(self, build_id: str, phase: str) -> None:
        self.db.execute("UPDATE problem_package_builds SET phase=? WHERE id=? AND status='running'", [phase, build_id])

    def mark_build_failed(self, build_id: str, error: str) -> None:
        self.db.execute(
            """UPDATE problem_package_builds SET status='failed',error=?,finished_at=?
               WHERE id=? AND status IN ('queued','running')""",
            [error, now_iso(), build_id],
        )

    @staticmethod
    def _delete_materialization_exports(
        connection,
        materialization_id: str,
    ) -> list[MaterializationExportRow]:
        rows = connection.execute(
            """SELECT export_type,archive_rel_path FROM exports
               WHERE materialization_id=?""",
            [materialization_id],
        ).fetchall()
        connection.execute(
            "DELETE FROM exports WHERE materialization_id=?",
            [materialization_id],
        )
        return [
            {
                "export_type": str(item["export_type"]),
                "archive_rel_path": str(item["archive_rel_path"]),
            }
            for item in rows
        ]

    def insert_materialization(
        self,
        row: MaterializationRow,
        *,
        build_id: str,
        invalidate_exports: bool = False,
    ) -> list[MaterializationExportRow]:
        def transaction(connection) -> list[MaterializationExportRow]:
            invalidated_exports: list[MaterializationExportRow] = []
            if invalidate_exports:
                invalidated_exports = self._delete_materialization_exports(
                    connection,
                    row["id"],
                )
            connection.execute(
                """INSERT INTO problem_package_materializations(
                   id,problem_id,source_commit,revision_number,source_digest,
                   archive_rel_path,archive_sha256,archive_size_bytes,
                   verification_id,status,created_at,checked_at,unavailable_reason
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(problem_id,source_commit) DO UPDATE SET
                   id=excluded.id,revision_number=excluded.revision_number,source_digest=excluded.source_digest,
                   archive_rel_path=excluded.archive_rel_path,archive_sha256=excluded.archive_sha256,
                   archive_size_bytes=excluded.archive_size_bytes,
                   verification_id=excluded.verification_id,status='available',checked_at=excluded.checked_at,
                   unavailable_reason=''""",
                [row[key] for key in (
                    "id", "problem_id", "source_commit", "revision_number", "source_digest",
                    "archive_rel_path", "archive_sha256", "archive_size_bytes",
                    "verification_id", "status",
                    "created_at", "checked_at", "unavailable_reason",
                )],
            )
            connection.execute(
                """UPDATE problem_package_builds
                   SET phase='complete',status='succeeded',materialization_id=?,
                       error='',finished_at=?
                   WHERE id=?""",
                [row["id"], now_iso(), build_id],
            )
            return invalidated_exports
        return self.db.write_transaction(transaction)

    def invalidate_materialization(
        self,
        materialization_id: str,
        reason: str,
    ) -> list[MaterializationExportRow]:
        def transaction(connection) -> list[MaterializationExportRow]:
            invalidated_exports = self._delete_materialization_exports(
                connection,
                materialization_id,
            )
            connection.execute(
                """UPDATE problem_package_materializations
                   SET status='unavailable',unavailable_reason=?,checked_at=? WHERE id=?""",
                [reason, now_iso(), materialization_id],
            )
            return invalidated_exports

        return self.db.write_transaction(transaction)

    def artifact_ref(self, verification_id: str, test_id: str, key: str) -> str:
        if key not in {"input_ref", "answer_ref"}:
            raise ValueError("invalid verification artifact key")
        row = self.db.fetch_one(
            f"""SELECT r.{key} AS artifact_ref FROM verification_tests_meta m
                JOIN verification_artifact_refs r ON r.verification_id=m.verification_id AND r.test_name=m.test_name
                WHERE m.verification_id=? AND m.source_id=? ORDER BY m.ordinal LIMIT 1""",
            [verification_id, test_id],
        )
        if row is None:
            row = self.db.fetch_one(
                f"SELECT {key} AS artifact_ref FROM verification_artifact_refs WHERE verification_id=? AND test_name=?",
                [verification_id, f"{test_id}.in"],
            )
        return "" if row is None else str(row["artifact_ref"] or "")

    def fail_interrupted_builds(self) -> int:
        def transaction(connection) -> int:
            cursor = connection.execute(
                """UPDATE problem_package_builds SET status='failed',phase='interrupted',
                   error='interrupted by application restart',finished_at=? WHERE status IN ('queued','running')""",
                [now_iso()],
            )
            return max(0, int(cursor.rowcount))
        return self.db.write_transaction(transaction)
