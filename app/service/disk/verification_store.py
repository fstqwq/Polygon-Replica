from __future__ import annotations

import secrets
from typing import TypedDict

from app.db import DB, now_iso
from app.service.platform.error_text import bounded_display_text
from app.service.verification.types import ACTIVE, Kind, Status


class VerificationStatusRow(TypedDict):
    id: str
    status: str

class VerificationRecordRow(TypedDict):
    id: str
    problem_id: int
    workspace_id: int | None
    signature: str
    source_commit: str
    kind: str
    status: str
    fail_reason: str
    error: str
    sanity_status: str
    created_at: str
    finished_at: str


class WorkspaceVerificationRow(TypedDict):
    id: str
    status: str
    signature: str
    source_commit: str
    kind: str
    fail_reason: str
    error: str
    sanity_status: str
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

    def _record_row(self, row: dict[str, object]) -> VerificationRecordRow:
        workspace_id_raw = row["workspace_id"]
        return {
            "id": str(row["id"] or ""),
            "problem_id": int(row["problem_id"]),
            "workspace_id": None if workspace_id_raw is None else int(workspace_id_raw),
            "signature": str(row["signature"] or ""),
            "source_commit": str(row["source_commit"] or ""),
            "kind": str(row["kind"] or ""),
            "status": str(row["status"] or ""),
            "fail_reason": str(row["fail_reason"] or ""),
            "error": str(row.get("error") or ""),
            "sanity_status": str(row.get("sanity_status") or ""),
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
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

    def record_row(self, verification_id: str) -> VerificationRecordRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,problem_id,workspace_id,signature,source_commit,kind,status,fail_reason,error,sanity_status,created_at,finished_at
            FROM verifications
            WHERE id=?
            """,
            [verification_id],
        )
        if row is None:
            return None
        return self._record_row(dict(row))

    def create_or_update_record(
        self,
        *,
        verification_id: str,
        problem_id: int,
        workspace_id: int | None,
        signature: str,
        source_commit: str,
        kind: str,
        status: str,
    ) -> None:
        now_text = now_iso()
        existing = self.db.fetch_one("SELECT id FROM verifications WHERE id=?", [verification_id])
        params = [
            int(problem_id),
            int(workspace_id) if workspace_id is not None else None,
            signature,
            source_commit,
            kind or Kind.ALL.value,
            status or Status.RUNNING.value,
        ]
        if existing is None:
            self.db.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,source_commit,kind,status,fail_reason,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    *params,
                    "",
                    now_text,
                    None,
                ],
            )
        else:
            self.db.execute(
                """
                UPDATE verifications
                SET problem_id=?,workspace_id=?,signature=?,source_commit=?,kind=?,status=?
                WHERE id=?
                """,
                [*params, verification_id],
            )

    def cancel_active_verification(self, verification_id: str, *, reason: str, now_text: str) -> bool:
        cancel_reason = bounded_display_text(reason or "verification cancelled by user")

        def _tx(conn) -> int:
            verification_row = conn.execute(
                """
                SELECT id,status
                FROM verifications
                WHERE id=? AND status IN (?,?,?)
                """,
                [verification_id, *ACTIVE],
            ).fetchone()
            if verification_row is None:
                return 0
            cursor = conn.execute(
                """
                UPDATE verifications
                SET status=?, fail_reason=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status IN (?,?,?)
                """,
                [
                    Status.FAILED.value,
                    cancel_reason,
                    now_text,
                    verification_id,
                    *ACTIVE,
                ],
            )
            return int(cursor.rowcount or 0)

        return int(self.db.write_transaction(_tx)) > 0

    def list_rows(
        self,
        *,
        problem_id: int,
        workspace_id: int,
        limit: int,
        kinds: tuple[str, ...],
    ) -> list[VerificationRecordRow]:
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.SAMPLE.value, Kind.CUSTOM.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        rows = self.db.fetch_all(
            f"""
            SELECT id,problem_id,workspace_id,signature,source_commit,kind,status,fail_reason,error,sanity_status,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND kind IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [int(problem_id), int(workspace_id), *kind_tokens, max(1, int(limit))],
        )
        return [self._record_row(dict(row)) for row in rows]

    def workspace_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
        ok_only: bool = False,
    ) -> list[WorkspaceVerificationRow]:
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.CUSTOM.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        sql = f"""
            SELECT id,status,signature,source_commit,kind,fail_reason,error,
                   sanity_status,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND kind IN ({placeholders})
        """
        params: list[object] = [problem_id, workspace_id, *kind_tokens]
        if ok_only:
            sql += " AND status='ok'"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.db.fetch_all(sql, params)
        return [
            {
                "id": str(row["id"]),
                "status": str(row["status"] or ""),
                "signature": str(row["signature"] or ""),
                "source_commit": str(row["source_commit"] or ""),
                "kind": str(row["kind"] or ""),
                "fail_reason": str(row["fail_reason"] or ""),
                "error": str(row["error"] or ""),
                "sanity_status": str(row["sanity_status"] or ""),
                "created_at": str(row["created_at"] or ""),
                "finished_at": str(row["finished_at"] or ""),
            }
            for row in rows
        ]

    def workspace_source_commit_verification_row(
        self,
        problem_id: int,
        workspace_id: int,
        source_commit: str,
        *,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
        ok_only: bool = False,
    ) -> WorkspaceVerificationRow | None:
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.CUSTOM.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        sql = f"""
            SELECT id,status,signature,source_commit,kind,fail_reason,error,
                   sanity_status,created_at,finished_at
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND source_commit=?
              AND kind IN ({placeholders})
        """
        params: list[object] = [problem_id, workspace_id, source_commit, *kind_tokens]
        if ok_only:
            sql += " AND status='ok'"
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self.db.fetch_one(sql, params)
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"] or ""),
            "signature": str(row["signature"] or ""),
            "source_commit": str(row["source_commit"] or ""),
            "kind": str(row["kind"] or ""),
            "fail_reason": str(row["fail_reason"] or ""),
            "error": str(row["error"] or ""),
            "sanity_status": str(row["sanity_status"] or ""),
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    def workspace_verification_row(
        self,
        problem_id: int,
        workspace_id: int,
        verification_id: str,
    ) -> WorkspaceVerificationRow | None:
        row = self.db.fetch_one(
            """
            SELECT id,status,signature,source_commit,kind,fail_reason,error,sanity_status,created_at,finished_at
            FROM verifications
            WHERE id=? AND problem_id=? AND workspace_id=?
            """,
            [verification_id, problem_id, workspace_id],
        )
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "status": str(row["status"] or ""),
            "signature": str(row["signature"] or ""),
            "source_commit": str(row["source_commit"] or ""),
            "kind": str(row["kind"] or ""),
            "fail_reason": str(row["fail_reason"] or ""),
            "error": str(row["error"] or ""),
            "sanity_status": str(row["sanity_status"] or ""),
            "created_at": str(row["created_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    def workspace_verification_exists(self, problem_id: int, workspace_id: int, verification_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT id FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
            [verification_id, problem_id, workspace_id],
        )
        return row is not None

    def workspace_artifact_exists(self, problem_id: int, workspace_id: int, artifact_id: str) -> bool:
        if artifact_id.startswith("p-"):
            row = self.db.fetch_one(
                "SELECT id FROM previews WHERE id=? AND problem_id=? AND workspace_id=?",
                [artifact_id, problem_id, workspace_id],
            )
            return row is not None
        row = self.db.fetch_one(
            "SELECT id FROM verifications WHERE id=? AND problem_id=? AND workspace_id=?",
            [artifact_id, problem_id, workspace_id],
        )
        return row is not None

    def latest_problem_verification_id_for_signature(self, problem_id: int, signature: str) -> str:
        row = self.db.fetch_one(
            """
            SELECT id
            FROM verifications
            WHERE problem_id=? AND signature=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [problem_id, signature],
        )
        if row is None:
            return ""
        return str(row["id"])

    def latest_workspace_verification_id_for_signature(
        self,
        problem_id: int,
        workspace_id: int,
        signature: str,
        *,
        ok_only: bool = False,
    ) -> str:
        sql = """
            SELECT id
            FROM verifications
            WHERE problem_id=? AND workspace_id=? AND signature=?
        """
        params: list[object] = [int(problem_id), int(workspace_id), signature]
        if ok_only:
            sql += " AND status='ok'"
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = self.db.fetch_one(sql, params)
        if row is None:
            return ""
        return str(row["id"] or "")

    def update_record_status(
        self,
        verification_id: str,
        *,
        status: str,
        fail_reason: str,
        finished: bool,
    ) -> None:
        safe_fail_reason = bounded_display_text(fail_reason)
        if finished:
            self.db.execute(
                "UPDATE verifications SET status=?, fail_reason=?, finished_at=? WHERE id=?",
                [status, safe_fail_reason, now_iso(), verification_id],
            )
            return
        self.db.execute(
            "UPDATE verifications SET status=?, fail_reason=?, finished_at=NULL WHERE id=?",
            [status, safe_fail_reason, verification_id],
        )
