from __future__ import annotations

from typing import TypedDict

from app.db import DB, now_iso


class WorkspaceExportRow(TypedDict):
    id: str
    verification_id: str
    export_type: str
    filename: str
    sha256: str
    size_bytes: int
    source_commit: str
    created_at: str


class ExportAuditRow(TypedDict):
    created_at: str
    details_json: str


class WorkspaceExportContext(TypedDict):
    username: str
    path: str


class ProblemExportRow(TypedDict):
    id: int
    slug: str
    name: str


class DuplicateExportRow(TypedDict):
    id: str
    filename: str


class ExportArchiveRow(TypedDict):
    filename: str
    export_type: str


class ExportStore:
    def __init__(self, db: DB):
        self.db = db

    def latest_workspace_source_commit(self, problem_id: int, workspace_id: int) -> str:
        row = self.db.fetch_one(
            """
            SELECT source_commit
            FROM exports
            WHERE problem_id=? AND workspace_id=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, workspace_id],
        )
        if row is None:
            return ""
        return str(row["source_commit"] or "")

    def download_source_commit(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
        filename: str,
    ) -> str:
        row = self.db.fetch_one(
            """
            SELECT source_commit
            FROM exports
            WHERE problem_id=? AND workspace_id=? AND verification_id=? AND filename=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, workspace_id, verification_id, filename],
        )
        if row is None:
            return ""
        return str(row["source_commit"] or "")

    def workspace_exports(self, problem_id: int, workspace_id: int, *, limit: int) -> list[WorkspaceExportRow]:
        rows = self.db.fetch_all(
            """
            SELECT id,verification_id,export_type,filename,sha256,size_bytes,source_commit,created_at
            FROM exports
            WHERE problem_id=? AND workspace_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [problem_id, workspace_id, max(1, int(limit))],
        )
        items: list[WorkspaceExportRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "verification_id": str(row["verification_id"] or ""),
                    "export_type": str(row["export_type"] or ""),
                    "filename": str(row["filename"] or ""),
                    "sha256": str(row["sha256"] or ""),
                    "size_bytes": int(row["size_bytes"] or 0),
                    "source_commit": str(row["source_commit"] or ""),
                    "created_at": str(row["created_at"] or ""),
                }
            )
        return items

    def export_audit_rows(self, problem_id: int, actor_user_id: int, *, limit: int) -> list[ExportAuditRow]:
        rows = self.db.fetch_all(
            """
            SELECT created_at,details_json
            FROM audit_log
            WHERE problem_id=? AND actor_user_id=? AND action='export.create'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [problem_id, actor_user_id, max(1, int(limit))],
        )
        items: list[ExportAuditRow] = []
        for row in rows:
            items.append(
                {
                    "created_at": str(row["created_at"] or ""),
                    "details_json": str(row["details_json"] or ""),
                }
            )
        return items

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

    def duplicate_exports(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        export_type: str,
        source_commit: str,
        keep_export_id: str,
    ) -> list[DuplicateExportRow]:
        rows = self.db.fetch_all(
            """
            SELECT exports.id,exports.filename
            FROM exports
            WHERE exports.problem_id=? AND exports.workspace_id=? AND exports.export_type=? AND exports.source_commit=? AND exports.id<>?
            ORDER BY exports.created_at DESC
            """,
            [int(problem_id), int(workspace_id), export_type, source_commit, keep_export_id],
        )
        items: list[DuplicateExportRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"] or ""),
                    "filename": str(row["filename"] or ""),
                }
            )
        return items

    def delete_export(self, export_id: str) -> None:
        self.db.execute("DELETE FROM exports WHERE id=?", [export_id])

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
        row = self.db.fetch_one("SELECT id,slug,name FROM problems WHERE slug=?", [slug])
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "slug": str(row["slug"] or ""),
            "name": str(row["name"] or ""),
        }

    def problem_workspace_id_by_slug(self, slug: str) -> int | None:
        row = self.db.fetch_one(
            """
            SELECT w.id
            FROM problems p
            JOIN workspaces w ON w.problem_id=p.id
            JOIN users u ON u.id=w.user_id
            WHERE p.slug=? AND u.username=substr(p.slug, 1, instr(p.slug, '/') - 1)
            LIMIT 1
            """,
            [slug],
        )
        if row is None:
            return None
        return int(row["id"])

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
