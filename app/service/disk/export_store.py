"""Export job and converted artifact persistence."""

from typing import TypedDict

from app.db import DB, now_iso


class ExportJobRow(TypedDict):
    id: str
    problem_id: int
    actor_user_id: int
    export_type: str
    source_commit: str
    status: str
    materialization_id: str
    export_id: str
    error: str
    created_at: str
    started_at: str
    finished_at: str
    filename: str
    sha256: str
    size_bytes: int


class ProblemExportRow(TypedDict):
    id: int
    slug: str


class WorkspaceSnapshotContext(TypedDict):
    username: str
    path: str


class ExportArchiveRow(TypedDict):
    filename: str
    export_type: str
    archive_rel_path: str
    materialization_id: str
    sha256: str
    size_bytes: int


class MaterializationPackageRow(TypedDict):
    export_id: str
    materialization_id: str
    export_type: str
    filename: str


def _job_row(row) -> ExportJobRow:
    return {
        "id": str(row["id"]),
        "problem_id": int(row["problem_id"]),
        "actor_user_id": int(row["actor_user_id"]),
        "export_type": str(row["export_type"]),
        "source_commit": str(row["source_commit"]),
        "status": str(row["status"]),
        "materialization_id": str(row["materialization_id"] or ""),
        "export_id": str(row["export_id"] or ""),
        "error": str(row["error"]),
        "created_at": str(row["created_at"]),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "filename": str(row["filename"] or ""),
        "sha256": str(row["sha256"] or ""),
        "size_bytes": int(row["size_bytes"] or 0),
    }


class ExportStore:
    def __init__(
        self,
        db: DB,
        *,
        job_formats: tuple[str, ...],
        package_formats: tuple[str, ...],
    ) -> None:
        if not job_formats:
            raise ValueError("at least one export job format is required")
        if not package_formats:
            raise ValueError("at least one package format is required")
        self.db = db
        self._job_formats = job_formats
        self._package_formats = package_formats
        self._job_format_placeholders = ",".join("?" for _ in job_formats)
        self._package_format_placeholders = ",".join("?" for _ in package_formats)

    def latest_succeeded_export_job(
        self,
        problem_id: int,
        source_commit: str,
        export_type: str,
    ) -> ExportJobRow | None:
        if export_type not in self._package_formats:
            return None
        row = self.db.fetch_one(
            """SELECT j.*,e.filename,e.sha256,e.size_bytes
               FROM export_jobs j JOIN exports e ON e.id=j.export_id
               WHERE j.problem_id=? AND j.source_commit=? AND j.export_type=?
                 AND j.status='succeeded'
               ORDER BY j.finished_at DESC,j.created_at DESC,j.id DESC LIMIT 1""",
            [int(problem_id), source_commit, export_type],
        )
        return None if row is None else _job_row(row)

    def problem_export_jobs(
        self,
        problem_id: int,
        *,
        limit: int,
    ) -> list[ExportJobRow]:
        params: list[object] = [
            int(problem_id),
            *self._job_formats,
            max(1, int(limit)),
        ]
        rows = self.db.fetch_all(
            """SELECT j.*,e.filename,e.sha256,e.size_bytes
               FROM export_jobs j LEFT JOIN exports e ON e.id=j.export_id
               WHERE j.problem_id=? AND j.export_type IN ("""
            + self._job_format_placeholders
            + ")"
            + """
               ORDER BY j.created_at DESC,j.id DESC LIMIT ?""",
            params,
        )
        return [_job_row(row) for row in rows]

    def materialization_packages(
        self,
        problem_id: int,
        materialization_ids: list[str],
    ) -> list[MaterializationPackageRow]:
        ids = list(dict.fromkeys(materialization_ids))
        result: list[MaterializationPackageRow] = []
        for offset in range(0, len(ids), 300):
            chunk = ids[offset : offset + 300]
            if not chunk:
                continue
            materialization_placeholders = ",".join("?" for _ in chunk)
            rows = self.db.fetch_all(
                """SELECT id,materialization_id,export_type,filename
                   FROM exports
                   WHERE problem_id=?
                     AND materialization_id IN ("""
                + materialization_placeholders
                + ") AND export_type IN ("
                + self._package_format_placeholders
                + ") ORDER BY materialization_id,export_type",
                [int(problem_id), *chunk, *self._package_formats],
            )
            result.extend(
                {
                    "export_id": str(row["id"]),
                    "materialization_id": str(row["materialization_id"]),
                    "export_type": str(row["export_type"]),
                    "filename": str(row["filename"]),
                }
                for row in rows
            )
        return result

    def export_job(
        self,
        problem_id: int,
        job_id: str,
    ) -> ExportJobRow | None:
        params: list[object] = [
            job_id,
            int(problem_id),
            *self._package_formats,
        ]
        row = self.db.fetch_one(
            """SELECT j.*,e.filename,e.sha256,e.size_bytes
               FROM export_jobs j LEFT JOIN exports e ON e.id=j.export_id
               WHERE j.id=? AND j.problem_id=? AND j.export_type IN ("""
            + self._package_format_placeholders
            + ")",
            params,
        )
        return None if row is None else _job_row(row)

    def create_export_job(
        self,
        *,
        job_id: str,
        problem_id: int,
        actor_user_id: int,
        export_type: str,
        source_commit: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO export_jobs(
               id,problem_id,actor_user_id,export_type,source_commit,status,
               materialization_id,export_id,error,created_at,started_at,finished_at
               ) VALUES(?,?,?,?,?,'queued',NULL,NULL,'',?,NULL,NULL)""",
            [job_id, int(problem_id), int(actor_user_id), export_type, source_commit, now_iso()],
        )

    def mark_export_job_running(self, job_id: str, *, source_commit: str) -> None:
        now = now_iso()

        def transaction(connection) -> None:
            row = connection.execute("SELECT status FROM export_jobs WHERE id=?", [job_id]).fetchone()
            if row is None or str(row["status"]) != "queued":
                raise RuntimeError(f"export job is not queued: {job_id}")
            connection.execute(
                "UPDATE export_jobs SET status='running',source_commit=?,started_at=?,error='' WHERE id=?",
                [source_commit, now, job_id],
            )
        self.db.write_transaction(transaction)

    def mark_export_job_packaging(
        self,
        job_id: str,
        *,
        materialization_id: str,
    ) -> None:
        def transaction(connection) -> None:
            cursor = connection.execute(
                """UPDATE export_jobs SET materialization_id=?
                   WHERE id=? AND status='running'""",
                [materialization_id, job_id],
            )
            if int(cursor.rowcount or 0) != 1:
                raise RuntimeError(f"export job is not running: {job_id}")

        self.db.write_transaction(transaction)

    def mark_export_job_succeeded(
        self,
        job_id: str,
        *,
        materialization_id: str,
        export_id: str | None,
        warning: str,
    ) -> None:
        now = now_iso()

        def transaction(connection) -> None:
            row = connection.execute("SELECT status FROM export_jobs WHERE id=?", [job_id]).fetchone()
            if row is None or str(row["status"]) != "running":
                raise RuntimeError(f"export job is not running: {job_id}")
            connection.execute(
                """UPDATE export_jobs SET status='succeeded',materialization_id=?,
                   export_id=?,error=?,finished_at=? WHERE id=?""",
                [materialization_id, export_id, warning, now, job_id],
            )
        self.db.write_transaction(transaction)

    def mark_export_job_failed(self, job_id: str, error: str) -> None:
        self.db.execute(
            """UPDATE export_jobs SET status='failed',error=?,finished_at=?
               WHERE id=? AND status IN ('queued','running')""",
            [error, now_iso(), job_id],
        )

    def fail_interrupted_export_jobs(self) -> int:
        def transaction(connection) -> int:
            cursor = connection.execute(
                """UPDATE export_jobs SET status='failed',error='interrupted by application restart',finished_at=?
                   WHERE status IN ('queued','running')
                     AND export_type IN ("""
                + self._job_format_placeholders
                + ")",
                [now_iso(), *self._job_formats],
            )
            return max(0, int(cursor.rowcount))
        return self.db.write_transaction(transaction)

    def export_archive_row(
        self, problem_id: int, export_id: str
    ) -> ExportArchiveRow | None:
        row = self.db.fetch_one(
            """SELECT filename,export_type,archive_rel_path,materialization_id,sha256,size_bytes
               FROM exports
               WHERE id=? AND problem_id=? AND export_type IN ("""
            + self._package_format_placeholders
            + ")",
            [export_id, int(problem_id), *self._package_formats],
        )
        if row is None:
            return None
        return {
            "filename": str(row["filename"]),
            "export_type": str(row["export_type"]),
            "archive_rel_path": str(row["archive_rel_path"]),
            "materialization_id": str(row["materialization_id"]),
            "sha256": str(row["sha256"]),
            "size_bytes": int(row["size_bytes"]),
        }

    def export_problem(self, export_id: str) -> dict[str, object] | None:
        row = self.db.fetch_one(
            """SELECT id,problem_id FROM exports
               WHERE id=? AND export_type IN ("""
            + self._package_format_placeholders
            + ")",
            [export_id, *self._package_formats],
        )
        return None if row is None else dict(row)

    def workspace_export_context(self, workspace_id: int) -> WorkspaceSnapshotContext | None:
        row = self.db.fetch_one(
            "SELECT w.path,u.username FROM workspaces w JOIN users u ON u.id=w.user_id WHERE w.id=?",
            [int(workspace_id)],
        )
        if row is None:
            return None
        return {"username": str(row["username"]), "path": str(row["path"])}

    def problem_export_row(self, slug: str) -> ProblemExportRow | None:
        row = self.db.fetch_one("SELECT id,slug FROM problems WHERE slug=?", [slug])
        if row is None:
            return None
        return {"id": int(row["id"]), "slug": str(row["slug"])}

    def cached_export(
        self,
        *,
        materialization_id: str,
        export_type: str,
    ) -> str:
        if export_type not in self._package_formats:
            return ""
        row = self.db.fetch_one(
            "SELECT id FROM exports WHERE materialization_id=? AND export_type=?",
            [materialization_id, export_type],
        )
        return "" if row is None else str(row["id"])

    def delete_export(self, export_id: str) -> None:
        self.db.execute("DELETE FROM exports WHERE id=?", [export_id])

    def insert_export_record(
        self,
        *,
        export_id: str,
        problem_id: int,
        materialization_id: str,
        export_type: str,
        filename: str,
        archive_rel_path: str,
        sha256: str,
        size_bytes: int,
        source_commit: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO exports(
               id,problem_id,materialization_id,export_type,
               filename,archive_rel_path,sha256,size_bytes,source_commit,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [
                export_id, int(problem_id), materialization_id, export_type,
                filename, archive_rel_path,
                sha256, int(size_bytes), source_commit, now_iso(),
            ],
        )
