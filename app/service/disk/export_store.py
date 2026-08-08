"""Export job and converted artifact persistence."""

from __future__ import annotations

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
    def __init__(self, db: DB):
        self.db = db

    def latest_source_commit(self, problem_id: int) -> str:
        row = self.db.fetch_one(
            """SELECT source_commit FROM export_jobs
               WHERE problem_id=? AND status='succeeded'
               ORDER BY finished_at DESC,created_at DESC,id DESC LIMIT 1""",
            [int(problem_id)],
        )
        return "" if row is None else str(row["source_commit"])

    def latest_succeeded_export_job(
        self,
        problem_id: int,
        source_commit: str,
        export_type: str,
    ) -> ExportJobRow | None:
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
        actor_user_id: int,
        *,
        limit: int,
        include_all: bool = False,
    ) -> list[ExportJobRow]:
        actor_clause = "" if include_all else "AND (j.actor_user_id=? OR j.status='succeeded')"
        params: list[object] = [int(problem_id)]
        if not include_all:
            params.append(int(actor_user_id))
        params.append(max(1, int(limit)))
        rows = self.db.fetch_all(
            f"""SELECT j.*,e.filename,e.sha256,e.size_bytes
               FROM export_jobs j LEFT JOIN exports e ON e.id=j.export_id
               WHERE j.problem_id=? {actor_clause}
               ORDER BY j.created_at DESC,j.id DESC LIMIT ?""",
            params,
        )
        return [_job_row(row) for row in rows]

    def export_job(
        self,
        problem_id: int,
        actor_user_id: int,
        job_id: str,
        *,
        include_all: bool = False,
    ) -> ExportJobRow | None:
        actor_clause = "" if include_all else "AND (j.actor_user_id=? OR j.status='succeeded')"
        params: list[object] = [job_id, int(problem_id)]
        if not include_all:
            params.append(int(actor_user_id))
        row = self.db.fetch_one(
            f"""SELECT j.*,e.filename,e.sha256,e.size_bytes
               FROM export_jobs j LEFT JOIN exports e ON e.id=j.export_id
               WHERE j.id=? AND j.problem_id=? {actor_clause}""",
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

    def mark_export_job_succeeded(
        self, job_id: str, *, materialization_id: str, export_id: str
    ) -> None:
        now = now_iso()

        def transaction(connection) -> None:
            row = connection.execute("SELECT status FROM export_jobs WHERE id=?", [job_id]).fetchone()
            if row is None or str(row["status"]) != "running":
                raise RuntimeError(f"export job is not running: {job_id}")
            connection.execute(
                """UPDATE export_jobs SET status='succeeded',materialization_id=?,
                   export_id=?,error='',finished_at=? WHERE id=?""",
                [materialization_id, export_id, now, job_id],
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
                   WHERE status IN ('queued','running')""",
                [now_iso()],
            )
            return max(0, int(cursor.rowcount))
        return self.db.write_transaction(transaction)

    def export_archive_row(
        self, problem_id: int, export_id: str
    ) -> ExportArchiveRow | None:
        row = self.db.fetch_one(
            """SELECT filename,export_type,archive_rel_path,materialization_id,sha256,size_bytes
               FROM exports WHERE id=? AND problem_id=?""",
            [export_id, int(problem_id)],
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
        options_hash: str,
    ) -> str:
        row = self.db.fetch_one(
            """SELECT id FROM exports WHERE materialization_id=? AND export_type=?
               AND options_hash=?""",
            [materialization_id, export_type, options_hash],
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
        options_hash: str,
        filename: str,
        archive_rel_path: str,
        sha256: str,
        size_bytes: int,
        source_commit: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO exports(
               id,problem_id,materialization_id,export_type,options_hash,
               filename,archive_rel_path,sha256,size_bytes,source_commit,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            [
                export_id, int(problem_id), materialization_id, export_type,
                options_hash, filename, archive_rel_path,
                sha256, int(size_bytes), source_commit, now_iso(),
            ],
        )
