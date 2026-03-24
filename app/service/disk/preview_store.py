from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from app.db import DB, now_iso


class PreviewRow(TypedDict):
    id: str
    status: str
    source_commit: str
    source_ref: str
    summary: dict[str, object]
    created_at: str
    finished_at: str


class PreviewArtifactRow(TypedDict):
    id: str
    status: str
    source_commit: str
    source_ref: str
    summary: dict[str, object]
    artifact_path: str


class PreviewLatestRow(TypedDict):
    id: str
    status: str
    created_at: str
    finished_at: str


class PreviewCacheRow(TypedDict):
    id: str
    summary: dict[str, object]


class PreviewStore:
    def __init__(self, db: DB):
        self.db = db

    def _summary_path(self, artifact_path: str) -> Path:
        return (Path(artifact_path).resolve() / "summary.json").resolve()

    def _read_summary(self, artifact_path: str) -> dict[str, object]:
        if not artifact_path:
            return {}
        try:
            text = self._summary_path(artifact_path).read_text(encoding="utf-8")
        except OSError:
            return {}
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_summary(self, artifact_path: str, summary: dict[str, object]) -> None:
        if not artifact_path:
            return
        path = self._summary_path(artifact_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def list_workspace_previews(self, problem_id: int, workspace_id: int, *, limit: int = 30) -> list[PreviewRow]:
        rows = self.db.fetch_all(
            """
            SELECT id,status,source_commit,source_ref,artifact_path,created_at,finished_at
            FROM previews
            WHERE problem_id=? AND workspace_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [problem_id, workspace_id, max(1, int(limit))],
        )
        result: list[PreviewRow] = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"]),
                    "status": str(row["status"]),
                    "source_commit": str(row["source_commit"] or ""),
                    "source_ref": str(row["source_ref"] or ""),
                    "summary": self._read_summary(str(row["artifact_path"] or "")),
                    "created_at": str(row["created_at"]),
                    "finished_at": str(row["finished_at"] or ""),
                }
            )
        return result

    def get_workspace_preview(self, problem_id: int, workspace_id: int, preview_id: str) -> PreviewRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,source_commit,source_ref,artifact_path,created_at,finished_at
            FROM previews
            WHERE id=? AND problem_id=? AND workspace_id=?
            """,
            [preview_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "summary": self._read_summary(str(row["artifact_path"] or "")),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"] or ""),
        }

    def get_workspace_preview_artifact(self, problem_id: int, workspace_id: int, preview_id: str) -> PreviewArtifactRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,source_commit,source_ref,artifact_path
            FROM previews
            WHERE id=? AND problem_id=? AND workspace_id=?
            """,
            [preview_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "summary": self._read_summary(str(row["artifact_path"] or "")),
            "artifact_path": str(row["artifact_path"] or ""),
        }

    def get_latest_workspace_preview(self, problem_id: int, workspace_id: int) -> PreviewLatestRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,created_at,finished_at
            FROM previews
            WHERE problem_id=? AND workspace_id=?
            ORDER BY created_at DESC,id DESC
            LIMIT 1
            """,
            [problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"] or ""),
        }

    def get_preview_row(self, preview_id: str) -> PreviewArtifactRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,source_commit,source_ref,artifact_path
            FROM previews
            WHERE id=?
            """,
            [preview_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "summary": self._read_summary(str(row["artifact_path"] or "")),
            "artifact_path": str(row["artifact_path"] or ""),
        }

    def list_cached_ok_previews(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        source_commit: str | None,
        limit: int,
    ) -> list[PreviewCacheRow]:
        sql = (
            "SELECT id,artifact_path FROM previews "
            "WHERE problem_id=? AND workspace_id=? AND status='ok'"
        )
        params: list[object] = [int(problem_id), int(workspace_id)]
        if source_commit is not None:
            sql += " AND source_commit=?"
            params.append(source_commit)
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.db.fetch_all(sql, params)
        result: list[PreviewCacheRow] = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"] or ""),
                    "summary": self._read_summary(str(row["artifact_path"] or "")),
                }
            )
        return result

    def insert_running_preview(
        self,
        *,
        preview_id: str,
        problem_id: int,
        workspace_id: int,
        artifact_path: str,
        source_commit: str,
        source_ref: str,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO previews(id,problem_id,workspace_id,verification_id,source_commit,source_ref,status,artifact_path,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [preview_id, problem_id, workspace_id, None, source_commit, source_ref, "running", artifact_path, now_iso()],
        )

    def update_preview_result(
        self,
        *,
        preview_id: str,
        verification_id: str | None,
        source_commit: str,
        source_ref: str,
        status: str,
        summary: dict[str, object],
    ) -> None:
        row = self.db.fetch_one("SELECT artifact_path FROM previews WHERE id=?", [preview_id])
        artifact_path = "" if row is None else str(row["artifact_path"] or "")
        self._write_summary(artifact_path, summary)
        self.db.execute(
            """
            UPDATE previews
            SET verification_id=?, source_commit=?, source_ref=?, status=?, finished_at=?
            WHERE id=?
            """,
            [verification_id, source_commit, source_ref, status, now_iso(), preview_id],
        )

    def list_other_workspace_previews(self, problem_id: int, workspace_id: int, keep_preview_id: str) -> list[PreviewArtifactRow]:
        rows = self.db.fetch_all(
            """
            SELECT id,status,source_commit,source_ref,artifact_path
            FROM previews
            WHERE problem_id=? AND workspace_id=? AND id<>?
            """,
            [problem_id, workspace_id, keep_preview_id],
        )
        result: list[PreviewArtifactRow] = []
        for row in rows:
            result.append(
                {
                    "id": str(row["id"]),
                    "status": str(row["status"]),
                    "source_commit": str(row["source_commit"] or ""),
                    "source_ref": str(row["source_ref"] or ""),
                    "summary": self._read_summary(str(row["artifact_path"] or "")),
                    "artifact_path": str(row["artifact_path"] or ""),
                }
            )
        return result

    def artifact_path_has_other_preview_ref(self, artifact_path: str, preview_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT 1 FROM previews WHERE artifact_path=? AND id<>? LIMIT 1",
            [artifact_path, preview_id],
        )
        return row is not None

    def delete_previews(self, problem_id: int, workspace_id: int, preview_ids: list[str]) -> None:
        if not preview_ids:
            return
        placeholders = ",".join(["?"] * len(preview_ids))
        self.db.execute(
            f"DELETE FROM previews WHERE problem_id=? AND workspace_id=? AND id IN ({placeholders})",
            [problem_id, workspace_id, *preview_ids],
        )
