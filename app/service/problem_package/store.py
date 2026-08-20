"""SQLite persistence for Native Package archives and their builds."""

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


class VerificationCertificationRow(TypedDict):
    id: str
    problem_id: int
    source_commit: str
    kind: str
    status: str


class NativePackageTestExecutionRow(TypedDict):
    source_id: str
    test_name: str
    ordinal: int
    final_status: str
    result_json: str
    input_ref: str


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

    def problems(self, problem_ids: list[int]) -> dict[int, PublishedProblem]:
        ids = list(dict.fromkeys(int(problem_id) for problem_id in problem_ids))
        result: dict[int, PublishedProblem] = {}
        for offset in range(0, len(ids), 300):
            chunk = ids[offset : offset + 300]
            if not chunk:
                continue
            placeholders = ",".join("?" for _problem_id in chunk)
            rows = self.db.fetch_all(
                f"SELECT id,slug,repo_name FROM problems WHERE id IN ({placeholders})",
                chunk,
            )
            for row in rows:
                problem_id = int(row["id"])
                result[problem_id] = {
                    "id": problem_id,
                    "slug": str(row["slug"]),
                    "repo_name": str(row["repo_name"]),
                }
        return result

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

    def available_native_package_history(
        self,
        problem_id: int,
        *,
        limit: int,
    ) -> list[MaterializationRow]:
        rows = self.db.fetch_all(
            """
            SELECT * FROM problem_package_materializations
            WHERE problem_id=? AND status='available'
            ORDER BY revision_number DESC,created_at DESC,id DESC
            LIMIT ?
            """,
            [int(problem_id), max(1, int(limit))],
        )
        return [_materialization(row) for row in rows]

    def materializations_for_revisions(
        self,
        revisions: list[tuple[int, str]],
    ) -> dict[tuple[int, str], MaterializationRow]:
        keys = list(
            dict.fromkeys(
                (int(problem_id), source_commit)
                for problem_id, source_commit in revisions
            )
        )
        result: dict[tuple[int, str], MaterializationRow] = {}
        for offset in range(0, len(keys), 300):
            chunk = keys[offset : offset + 300]
            if not chunk:
                continue
            values = ",".join("(?,?)" for _key in chunk)
            params = [value for key in chunk for value in key]
            rows = self.db.fetch_all(
                f"""
                WITH requested(problem_id,source_commit) AS (VALUES {values})
                SELECT m.*
                FROM problem_package_materializations m
                JOIN requested r
                  ON r.problem_id=m.problem_id
                 AND r.source_commit=m.source_commit
                """,
                params,
            )
            for row in rows:
                materialization = _materialization(row)
                key = (
                    materialization["problem_id"],
                    materialization["source_commit"],
                )
                result[key] = materialization
        return result

    def verification_certifications(
        self,
        verification_ids: list[str],
    ) -> dict[str, VerificationCertificationRow]:
        ids = list(dict.fromkeys(item for item in verification_ids if item))
        result: dict[str, VerificationCertificationRow] = {}
        for offset in range(0, len(ids), 300):
            chunk = ids[offset : offset + 300]
            if not chunk:
                continue
            placeholders = ",".join("?" for _verification_id in chunk)
            rows = self.db.fetch_all(
                f"""
                SELECT id,problem_id,source_commit,kind,status
                FROM verifications
                WHERE id IN ({placeholders})
                """,
                chunk,
            )
            for row in rows:
                verification_id = str(row["id"])
                result[verification_id] = {
                    "id": verification_id,
                    "problem_id": int(row["problem_id"]),
                    "source_commit": str(row["source_commit"]),
                    "kind": str(row["kind"]),
                    "status": str(row["status"]),
                }
        return result

    def latest_available_materializations_before(
        self,
        revisions: list[tuple[int, int]],
    ) -> dict[int, MaterializationRow]:
        keys = list(
            dict.fromkeys(
                (int(problem_id), int(revision_number))
                for problem_id, revision_number in revisions
            )
        )
        result: dict[int, MaterializationRow] = {}
        for offset in range(0, len(keys), 300):
            chunk = keys[offset : offset + 300]
            if not chunk:
                continue
            values = ",".join("(?,?)" for _key in chunk)
            params = [value for key in chunk for value in key]
            rows = self.db.fetch_all(
                f"""
                WITH requested(problem_id,published_revision_number) AS (VALUES {values}),
                candidates AS (
                    SELECT m.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY m.problem_id
                               ORDER BY m.revision_number DESC, m.created_at DESC
                           ) AS position
                    FROM problem_package_materializations m
                    JOIN requested r ON r.problem_id=m.problem_id
                    WHERE m.status='available'
                      AND m.revision_number < r.published_revision_number
                )
                SELECT * FROM candidates WHERE position=1
                """,
                params,
            )
            for row in rows:
                materialization = _materialization(row)
                result[materialization["problem_id"]] = materialization
        return result

    def active_builds_for_revisions(
        self,
        revisions: list[tuple[int, str]],
    ) -> set[tuple[int, str]]:
        keys = list(
            dict.fromkeys(
                (int(problem_id), source_commit)
                for problem_id, source_commit in revisions
                if source_commit
            )
        )
        result: set[tuple[int, str]] = set()
        for offset in range(0, len(keys), 300):
            chunk = keys[offset : offset + 300]
            if not chunk:
                continue
            values = ",".join("(?,?)" for _key in chunk)
            params = [value for key in chunk for value in key]
            rows = self.db.fetch_all(
                f"""
                WITH requested(problem_id,source_commit) AS (VALUES {values})
                SELECT b.problem_id,b.source_commit
                FROM problem_package_builds b
                JOIN requested r
                  ON r.problem_id=b.problem_id
                 AND r.source_commit=b.source_commit
                WHERE b.status IN ('queued','running')
                """,
                params,
            )
            result.update(
                (int(row["problem_id"]), str(row["source_commit"]))
                for row in rows
            )
        return result

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
        def transaction(connection) -> None:
            cursor = connection.execute(
                """UPDATE problem_package_builds
                   SET status='running',phase=?,started_at=?
                   WHERE id=? AND status='queued'""",
                [phase, now_iso(), build_id],
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError(f"problem package build is not queued: {build_id}")

        self.db.write_transaction(transaction)

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

    def update_materialization_verification(
        self,
        materialization_id: str,
        *,
        expected_verification_id: str,
        verification_id: str,
    ) -> bool:
        def transaction(connection) -> bool:
            cursor = connection.execute(
                """
                UPDATE problem_package_materializations
                SET verification_id=?,checked_at=?
                WHERE id=? AND verification_id=? AND status='available'
                """,
                [
                    verification_id,
                    now_iso(),
                    materialization_id,
                    expected_verification_id,
                ],
            )
            return int(cursor.rowcount or 0) == 1

        return self.db.write_transaction(transaction)

    def artifact_ref(self, verification_id: str, test_id: str, key: str) -> str:
        if key not in {"input_ref", "answer_ref"}:
            raise ValueError("invalid verification artifact key")
        role = "generated-input" if key == "input_ref" else "accepted-answer"
        row = self.db.fetch_one(
            """
            SELECT artifact.artifact_ref
            FROM verification_tests_meta metadata
            JOIN verification_task_artifacts artifact
              ON artifact.verification_id=metadata.verification_id
             AND artifact.test_name=metadata.test_name
             AND artifact.role=?
            WHERE metadata.verification_id=? AND metadata.source_id=?
            ORDER BY metadata.ordinal,artifact.task_id
            LIMIT 1
            """,
            [role, verification_id, test_id],
        )
        if row is None:
            row = self.db.fetch_one(
                """
                SELECT artifact_ref
                FROM verification_task_artifacts
                WHERE verification_id=? AND test_name=? AND role=?
                ORDER BY task_id
                LIMIT 1
                """,
                [verification_id, f"{test_id}.in", role],
            )
        return "" if row is None else str(row["artifact_ref"] or "")

    def test_execution_rows(
        self,
        verification_id: str,
    ) -> list[NativePackageTestExecutionRow]:
        rows = self.db.fetch_all(
            """
            SELECT metadata.source_id,metadata.test_name,metadata.ordinal,
                   task.final_status,task.result_json,
                   COALESCE(artifact.artifact_ref,'') AS input_ref
            FROM verification_tests_meta metadata
            JOIN verification_tasks task
              ON task.verification_id=metadata.verification_id
             AND task.test_name=metadata.test_name
             AND task.task_kind='generate-input'
            LEFT JOIN verification_task_artifacts artifact
              ON artifact.verification_id=task.verification_id
             AND artifact.task_id=task.id
             AND artifact.pass_number=0
             AND artifact.role='generated-input'
            WHERE metadata.verification_id=?
            ORDER BY metadata.ordinal,task.id
            """,
            [verification_id],
        )
        return [
            {
                "source_id": str(row["source_id"]),
                "test_name": str(row["test_name"]),
                "ordinal": int(row["ordinal"]),
                "final_status": str(row["final_status"]),
                "result_json": str(row["result_json"]),
                "input_ref": str(row["input_ref"]),
            }
            for row in rows
        ]

    def fail_interrupted_builds(self) -> int:
        def transaction(connection) -> int:
            cursor = connection.execute(
                """UPDATE problem_package_builds SET status='failed',phase='interrupted',
                   error='interrupted by application restart',finished_at=? WHERE status IN ('queued','running')""",
                [now_iso()],
            )
            return max(0, int(cursor.rowcount))
        return self.db.write_transaction(transaction)
