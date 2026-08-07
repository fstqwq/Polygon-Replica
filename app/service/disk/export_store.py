from __future__ import annotations

from typing import TypedDict

from app.db import DB, now_iso


class ExportJobRow(TypedDict):
    id: str
    problem_id: int
    workspace_id: int
    actor_user_id: int
    verification_id: str
    export_type: str
    source_commit: str
    status: str
    export_id: str
    error: str
    created_at: str
    started_at: str
    finished_at: str
    filename: str
    sha256: str
    size_bytes: int


class WorkspaceExportContext(TypedDict):
    username: str
    path: str


class ProblemExportRow(TypedDict):
    id: int
    slug: str


class ExportArchiveRow(TypedDict):
    filename: str
    export_type: str


class ExportStore:
    def __init__(self, db: DB):
        self.db = db

    def latest_workspace_source_commit(self, problem_id: int, workspace_id: int) -> str:
        row = self.db.fetch_one(
            """
            SELECT j.source_commit
            FROM export_jobs AS j
            JOIN exports AS e ON e.id=j.export_id
            WHERE j.problem_id=? AND j.workspace_id=? AND j.status='succeeded'
            ORDER BY j.finished_at DESC,j.created_at DESC,j.id DESC
            LIMIT 1
            """,
            [problem_id, workspace_id],
        )
        if row is None:
            return ""
        return str(row["source_commit"] or "")

    def workspace_export_jobs(
        self,
        problem_id: int,
        workspace_id: int,
        actor_user_id: int,
        *,
        limit: int,
    ) -> list[ExportJobRow]:
        rows = self.db.fetch_all(
            """
            SELECT
                j.id,j.problem_id,j.workspace_id,j.actor_user_id,j.verification_id,
                j.export_type,j.source_commit,j.status,j.export_id,j.error,
                j.created_at,j.started_at,j.finished_at,
                e.filename,e.sha256,e.size_bytes
            FROM export_jobs AS j
            LEFT JOIN exports AS e ON e.id=j.export_id
            WHERE j.problem_id=? AND j.workspace_id=? AND j.actor_user_id=?
            ORDER BY j.created_at DESC,j.id DESC
            LIMIT ?
            """,
            [problem_id, workspace_id, actor_user_id, max(1, int(limit))],
        )
        items: list[ExportJobRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "problem_id": int(row["problem_id"]),
                    "workspace_id": int(row["workspace_id"]),
                    "actor_user_id": int(row["actor_user_id"]),
                    "verification_id": str(row["verification_id"] or ""),
                    "export_type": str(row["export_type"] or ""),
                    "source_commit": str(row["source_commit"] or ""),
                    "status": str(row["status"] or ""),
                    "export_id": str(row["export_id"] or ""),
                    "error": str(row["error"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "started_at": str(row["started_at"] or ""),
                    "finished_at": str(row["finished_at"] or ""),
                    "filename": str(row["filename"] or ""),
                    "sha256": str(row["sha256"] or ""),
                    "size_bytes": int(row["size_bytes"] or 0),
                }
            )
        return items

    def export_job(
        self,
        problem_id: int,
        workspace_id: int,
        actor_user_id: int,
        job_id: str,
    ) -> ExportJobRow | None:
        rows = self.db.fetch_all(
            """
            SELECT
                j.id,j.problem_id,j.workspace_id,j.actor_user_id,j.verification_id,
                j.export_type,j.source_commit,j.status,j.export_id,j.error,
                j.created_at,j.started_at,j.finished_at,
                e.filename,e.sha256,e.size_bytes
            FROM export_jobs AS j
            LEFT JOIN exports AS e ON e.id=j.export_id
            WHERE j.id=? AND j.problem_id=? AND j.workspace_id=? AND j.actor_user_id=?
            LIMIT 1
            """,
            [job_id, problem_id, workspace_id, actor_user_id],
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "id": str(row["id"]),
            "problem_id": int(row["problem_id"]),
            "workspace_id": int(row["workspace_id"]),
            "actor_user_id": int(row["actor_user_id"]),
            "verification_id": str(row["verification_id"] or ""),
            "export_type": str(row["export_type"] or ""),
            "source_commit": str(row["source_commit"] or ""),
            "status": str(row["status"] or ""),
            "export_id": str(row["export_id"] or ""),
            "error": str(row["error"] or ""),
            "created_at": str(row["created_at"] or ""),
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
            "filename": str(row["filename"] or ""),
            "sha256": str(row["sha256"] or ""),
            "size_bytes": int(row["size_bytes"] or 0),
        }

    def create_export_job(
        self,
        *,
        job_id: str,
        problem_id: int,
        workspace_id: int,
        actor_user_id: int,
        verification_id: str,
        export_type: str,
        source_commit: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO export_jobs(
                id,problem_id,workspace_id,actor_user_id,verification_id,
                export_type,source_commit,status,export_id,error,
                created_at,started_at,finished_at
            )
            VALUES(?,?,?,?,?,?,?,'queued',NULL,'',?,NULL,NULL)
            """,
            [
                job_id,
                problem_id,
                workspace_id,
                actor_user_id,
                verification_id,
                export_type,
                source_commit,
                now_iso(),
            ],
        )

    def mark_export_job_running(
        self,
        job_id: str,
        *,
        verification_id: str,
        source_commit: str,
    ) -> None:
        now_text = now_iso()

        def transaction(connection) -> None:
            row = connection.execute(
                "SELECT status FROM export_jobs WHERE id=?",
                [job_id],
            ).fetchone()
            if row is None:
                raise RuntimeError(f"export job not found: {job_id}")
            if str(row["status"]) != "queued":
                raise RuntimeError(f"export job is not queued: {job_id}")
            connection.execute(
                """
                UPDATE export_jobs
                SET status='running',verification_id=?,source_commit=?,started_at=?,error=''
                WHERE id=?
                """,
                [verification_id, source_commit, now_text, job_id],
            )

        self.db.write_transaction(transaction)

    def mark_export_job_succeeded(
        self,
        job_id: str,
        *,
        verification_id: str,
        export_id: str,
    ) -> None:
        now_text = now_iso()

        def transaction(connection) -> None:
            row = connection.execute(
                "SELECT status FROM export_jobs WHERE id=?",
                [job_id],
            ).fetchone()
            if row is None:
                raise RuntimeError(f"export job not found: {job_id}")
            if str(row["status"]) != "running":
                raise RuntimeError(f"export job is not running: {job_id}")
            connection.execute(
                """
                UPDATE export_jobs
                SET status='succeeded',verification_id=?,export_id=?,error='',finished_at=?
                WHERE id=?
                """,
                [verification_id, export_id, now_text, job_id],
            )

        self.db.write_transaction(transaction)

    def mark_export_job_failed(self, job_id: str, error: str) -> None:
        now_text = now_iso()
        self.db.execute(
            """
            UPDATE export_jobs
            SET status='failed',error=?,finished_at=?
            WHERE id=? AND status IN ('queued','running')
            """,
            [error, now_text, job_id],
        )

    def fail_interrupted_export_jobs(self) -> int:
        now_text = now_iso()

        def transaction(connection) -> int:
            cursor = connection.execute(
                """
                UPDATE export_jobs
                SET status='failed',error='interrupted by application restart',finished_at=?
                WHERE status IN ('queued','running')
                """,
                [now_text],
            )
            return max(0, int(cursor.rowcount))

        return int(self.db.write_transaction(transaction))

    def workspace_export_context(self, workspace_id: int) -> WorkspaceExportContext | None:
        row = self.db.fetch_one(
            """
            SELECT w.path,u.username
            FROM workspaces w
            JOIN users u ON u.id=w.user_id
            WHERE w.id=?
            """,
            [int(workspace_id)],
        )
        if row is None:
            return None
        return {
            "username": str(row["username"] or ""),
            "path": str(row["path"] or ""),
        }

    def export_archive_row(self, problem_id: int, workspace_id: int, export_id: str) -> ExportArchiveRow | None:
        row = self.db.fetch_one(
            """
            SELECT filename,export_type
            FROM exports
            WHERE id=? AND problem_id=? AND workspace_id=?
            """,
            [export_id, int(problem_id), int(workspace_id)],
        )
        if row is None:
            return None
        return {
            "filename": str(row["filename"] or ""),
            "export_type": str(row["export_type"] or ""),
        }

    def problem_export_row(self, slug: str) -> ProblemExportRow | None:
        row = self.db.fetch_one("SELECT id,slug FROM problems WHERE slug=?", [slug])
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"] or ""),
        }

    def insert_export_record(
        self,
        *,
        export_id: str,
        problem_id: int,
        verification_id: str,
        workspace_id: int | None,
        export_type: str,
        filename: str,
        sha256: str,
        size_bytes: int,
        source_commit: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO exports(id,problem_id,verification_id,workspace_id,export_type,filename,sha256,size_bytes,source_commit,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            [
                export_id,
                int(problem_id),
                verification_id,
                workspace_id,
                export_type,
                filename,
                sha256,
                int(size_bytes),
                source_commit,
                now_iso(),
            ],
        )
