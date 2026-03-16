from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import TypedDict

from app.db import DB, now_iso
from app.service.platform.fs.layout import FsManager
from app.service.verification.summary import sanitize_verification_summary
from app.service.verification.types import ACTIVE, Kind, Status


class VerificationRuntimeRow(TypedDict):
    id: str
    status: str
    summary_json: str
    artifact_path: str


class VerificationStatusRow(TypedDict):
    id: str
    status: str


class WorkspaceVerificationListRow(TypedDict):
    id: str
    summary_json: str


class WorkspaceVerificationMetaRow(TypedDict):
    id: str
    status: str
    summary_json: str


class VerificationRecordRow(TypedDict):
    id: str
    problem_id: int
    workspace_id: int | None
    source_commit: str
    source_ref: str
    kind: str
    status: str
    summary_json: str
    artifact_path: str
    created_at: str
    finished_at: str


class WorkspaceVerificationStageRow(TypedDict):
    id: str
    status: str
    source_commit: str
    source_ref: str
    summary_json: str
    created_at: str
    finished_at: str


class VerificationStore:
    def __init__(self, db: DB):
        self.db = db

    def allocate_id(self) -> str:
        for _ in range(8):
            candidate = f"ver-{secrets.token_hex(6)}"
            if self.db.fetch_one("SELECT id FROM verifications WHERE id=?", [candidate]) is None:
                return candidate
        return f"ver-{secrets.token_hex(8)}"

    def get_runtime_row(self, problem_id: int, verification_id: str) -> VerificationRuntimeRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,summary_json,artifact_path
            FROM verifications
            WHERE id=? AND problem_id=?
            """,
            [verification_id, problem_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "summary_json": str(row["summary_json"] or ""),
            "artifact_path": str(row["artifact_path"] or ""),
        }

    def get_status_row(self, problem_id: int, verification_id: str) -> VerificationStatusRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status
            FROM verifications
            WHERE id=? AND problem_id=?
            """,
            [verification_id, problem_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
        }

    def status(self, verification_id: str) -> str:
        row = self.db.fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        if row is None:
            return ""
        return str(row["status"] or "")

    def wait_terminal_status(
        self,
        verification_id: str,
        *,
        timeout_sec: float,
        poll_sec: float,
    ) -> str:
        if not verification_id:
            return ""
        deadline = time.monotonic() + max(0.5, float(timeout_sec))
        while time.monotonic() < deadline:
            status = self.status(verification_id)
            if status in {Status.OK.value, Status.FAILED.value, Status.CANCELLED.value}:
                return status
            time.sleep(max(0.01, float(poll_sec)))
        return ""

    def record_row(self, verification_id: str) -> VerificationRecordRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
            FROM verifications
            WHERE id=?
            """,
            [verification_id],
        )
        if row is None:
            return None
        workspace_id_raw = row["workspace_id"]
        return {
            "id": str(row["id"]),
            "problem_id": int(row["problem_id"]),
            "workspace_id": None if workspace_id_raw is None else int(workspace_id_raw),
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "kind": str(row["kind"] or ""),
            "status": str(row["status"] or ""),
            "summary_json": str(row["summary_json"] or ""),
            "artifact_path": str(row["artifact_path"] or ""),
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    def create_or_update_record(
        self,
        fs_manager: FsManager,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        source_commit: str,
        source_ref: str,
        kind: str,
        status: str,
        summary_json: str,
        artifact_path: str | Path | None,
    ) -> str:
        existing = self.db.fetch_one("SELECT id,artifact_path FROM verifications WHERE id=?", [verification_id])
        if artifact_path is None:
            if existing is None:
                root = fs_manager.prepare_verification_root(verification_id).resolve()
            else:
                current_artifact_path = str(existing["artifact_path"] or "")
                root = fs_manager.prepare_verification_root(verification_id).resolve() if not current_artifact_path else Path(current_artifact_path).resolve()
        else:
            root = Path(artifact_path).resolve()
        root.mkdir(parents=True, exist_ok=True)
        now_text = now_iso()
        params = [
            int(problem_id),
            int(workspace_id) if workspace_id is not None else None,
            source_commit,
            source_ref,
            kind or Kind.VERIFICATION.value,
            status or Status.RUNNING.value,
            summary_json,
            str(root),
        ]
        if existing is None:
            self.db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [verification_id, *params, now_text, None],
            )
        else:
            self.db.execute(
                """
                UPDATE verifications
                SET problem_id=?,workspace_id=?,source_commit=?,source_ref=?,kind=?,status=?,summary_json=?,artifact_path=?
                WHERE id=?
                """,
                [*params, verification_id],
            )
        return str(root)

    def cancel_active_verification(self, verification_id: str, *, reason: str, now_text: str) -> bool:
        cancel_reason = reason or "verification cancelled by user"

        def _tx(conn) -> int:
            verification_row = conn.execute(
                """
                SELECT summary_json
                FROM verifications
                WHERE id=? AND kind=? AND status IN (?,?,?)
                """,
                [verification_id, Kind.VERIFICATION.value, *ACTIVE],
            ).fetchone()
            if verification_row is None:
                return 0
            summary_text = str(verification_row["summary_json"] or "")
            try:
                summary_payload = json.loads(summary_text) if summary_text else {}
            except Exception:
                summary_payload = {}
            if not isinstance(summary_payload, dict):
                summary_payload = {}
            summary_payload["cancelled"] = True
            summary_payload["cancel_reason"] = cancel_reason
            if not summary_payload.get("error"):
                summary_payload["error"] = cancel_reason
            cursor = conn.execute(
                """
                UPDATE verifications
                SET status=?, summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND kind=? AND status IN (?,?,?)
                """,
                [
                    Status.FAILED.value,
                    json.dumps(summary_payload),
                    now_text,
                    verification_id,
                    Kind.VERIFICATION.value,
                    *ACTIVE,
                ],
            )
            return int(cursor.rowcount or 0)

        return int(self.db.write_transaction(_tx)) > 0

    def save_summary_record(
        self,
        *,
        verification_id: str,
        status: str,
        summary_json: str,
        finished: bool,
    ) -> None:
        if finished:
            self.db.execute(
                "UPDATE verifications SET status=?, summary_json=?, finished_at=? WHERE id=?",
                [status or Status.FAILED.value, summary_json, now_iso(), verification_id],
            )
            return
        self.db.execute(
            "UPDATE verifications SET status=?, summary_json=?, finished_at=NULL WHERE id=?",
            [status or Status.RUNNING.value, summary_json, verification_id],
        )

    def update_source_identity(self, verification_id: str, *, source_commit: str, source_ref: str) -> None:
        self.db.execute(
            "UPDATE verifications SET source_commit=?, source_ref=? WHERE id=?",
            [source_commit, source_ref, verification_id],
        )

    def get_workspace_runtime_row(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> VerificationRuntimeRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,summary_json,artifact_path
            FROM verifications
            WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'
            """,
            [verification_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "summary_json": str(row["summary_json"] or ""),
            "artifact_path": str(row["artifact_path"] or ""),
        }

    def artifact_path_for_problem_artifact(self, problem_id: int, artifact_id: str) -> str:
        if artifact_id.startswith("p-"):
            row = self.db.fetch_one(
                "SELECT artifact_path FROM previews WHERE id=? AND problem_id=?",
                [artifact_id, problem_id],
            )
        else:
            row = self.db.fetch_one(
                """
                SELECT artifact_path FROM (
                    SELECT artifact_path
                    FROM verifications
                    WHERE id=? AND problem_id=?
                    UNION ALL
                    SELECT artifact_path
                    FROM previews
                    WHERE id=? AND problem_id=?
                )
                LIMIT 1
                """,
                [artifact_id, problem_id, artifact_id, problem_id],
            )
        if row is None:
            return ""
        return str(row["artifact_path"] or "")

    def artifact_path_for_verification(self, verification_id: str) -> str:
        row = self.db.fetch_one("SELECT artifact_path FROM verifications WHERE id=?", [verification_id])
        if row is None:
            return ""
        return str(row["artifact_path"] or "")

    def workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int,
        kinds: tuple[str, ...] = (Kind.VERIFICATION.value,),
    ) -> list[WorkspaceVerificationListRow]:
        kind_tokens = list(kinds) or [Kind.VERIFICATION.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        rows = self.db.fetch_all(
            f"""
            SELECT id,summary_json
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND kind IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [problem_id, workspace_id, *kind_tokens, max(1, int(limit))],
        )
        items: list[WorkspaceVerificationListRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "summary_json": str(row["summary_json"] or ""),
                }
            )
        return items

    def list_rows(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        limit: int,
        kinds: tuple[str, ...],
    ) -> list[VerificationRecordRow]:
        kind_tokens = list(kinds) or [Kind.VERIFICATION.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        rows = self.db.fetch_all(
            f"""
            SELECT id,problem_id,workspace_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND kind IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [int(problem_id), int(workspace_id), *kind_tokens, max(1, int(limit))],
        )
        items: list[VerificationRecordRow] = []
        for row in rows:
            workspace_id_raw = row["workspace_id"]
            items.append(
                {
                    "id": str(row["id"]),
                    "problem_id": int(row["problem_id"]),
                    "workspace_id": None if workspace_id_raw is None else int(workspace_id_raw),
                    "source_commit": str(row["source_commit"] or ""),
                    "source_ref": str(row["source_ref"] or ""),
                    "kind": str(row["kind"] or ""),
                    "status": str(row["status"] or ""),
                    "summary_json": str(row["summary_json"] or ""),
                    "artifact_path": str(row["artifact_path"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "finished_at": str(row["finished_at"] or ""),
                }
            )
        return items

    def workspace_stage_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int,
        ok_only: bool = False,
    ) -> list[WorkspaceVerificationStageRow]:
        sql = """
            SELECT id,status,source_commit,source_ref,summary_json,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND kind!='sample'
        """
        params: list[object] = [problem_id, workspace_id]
        if ok_only:
            sql += " AND status='ok'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.db.fetch_all(sql, params)
        items: list[WorkspaceVerificationStageRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "status": str(row["status"] or ""),
                    "source_commit": str(row["source_commit"] or ""),
                    "source_ref": str(row["source_ref"] or ""),
                    "summary_json": str(row["summary_json"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "finished_at": str(row["finished_at"] or ""),
                }
            )
        return items

    def workspace_committed_stage_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        source_commit: str,
        source_ref: str,
        limit: int,
        ok_only: bool = False,
    ) -> list[WorkspaceVerificationStageRow]:
        sql = """
            SELECT id,status,source_commit,source_ref,summary_json,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND source_commit=? AND source_ref=? AND kind!='sample'
        """
        params: list[object] = [problem_id, workspace_id, source_commit, source_ref]
        if ok_only:
            sql += " AND status='ok'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.db.fetch_all(sql, params)
        items: list[WorkspaceVerificationStageRow] = []
        for row in rows:
            items.append(
                {
                    "id": str(row["id"]),
                    "status": str(row["status"] or ""),
                    "source_commit": str(row["source_commit"] or ""),
                    "source_ref": str(row["source_ref"] or ""),
                    "summary_json": str(row["summary_json"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "finished_at": str(row["finished_at"] or ""),
                }
            )
        return items

    def workspace_stage_row(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> WorkspaceVerificationStageRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,source_commit,source_ref,summary_json,created_at,finished_at
            FROM verifications
            WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'
            """,
            [verification_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"] or ""),
            "source_commit": str(row["source_commit"] or ""),
            "source_ref": str(row["source_ref"] or ""),
            "summary_json": str(row["summary_json"] or ""),
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    def workspace_verification_exists(self, problem_id: int, workspace_id: int, verification_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT id FROM verifications WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'",
            [verification_id, problem_id, workspace_id],
        )
        return row is not None

    def workspace_artifact_exists(self, problem_id: int, workspace_id: int, artifact_id: str) -> bool:
        if artifact_id.startswith("p-"):
            row = self.db.fetch_one(
                "SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
                [artifact_id, problem_id, workspace_id],
            )
        else:
            row = self.db.fetch_one(
                """
                SELECT id FROM (
                    SELECT id
                    FROM verifications
                    WHERE id=? AND problem_id=? AND workspace_id=?
                    UNION ALL
                    SELECT id
                    FROM previews
                    WHERE id=? AND problem_id=? AND workspace_id=?
                )
                LIMIT 1
                """,
                [artifact_id, problem_id, workspace_id, artifact_id, problem_id, workspace_id],
            )
        return row is not None

    def workspace_verification_meta(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> WorkspaceVerificationMetaRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,summary_json
            FROM verifications
            WHERE id=? AND problem_id=? AND workspace_id=? AND kind='verification'
            """,
            [verification_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"]),
            "summary_json": str(row["summary_json"] or ""),
        }

    def latest_problem_verification_id_for_source_commit(self, problem_id: int, source_commit: str) -> str:
        row = self.db.fetch_one(
            """
            SELECT id
            FROM verifications
            WHERE problem_id=? AND source_commit=? AND kind='verification'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, source_commit],
        )
        if row is None:
            return ""
        return str(row["id"])

    def runtime_summary(self, verification_id: str) -> dict[str, object]:
        row = self.db.fetch_one("SELECT summary_json FROM verifications WHERE id=?", [verification_id])
        if row is None:
            return {}
        text = str(row["summary_json"] or "")
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        return sanitize_verification_summary(payload) if isinstance(payload, dict) else {}
