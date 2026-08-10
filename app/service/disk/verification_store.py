from __future__ import annotations

from typing import Mapping, TypedDict

from app.db import DB, now_iso
from app.service.verification.lifecycle import AdmissionCommit, VerificationAdmission
from app.service.verification.types import (
    Kind,
    Status,
    WorkspaceVerificationKey,
    WorkspaceVerificationRow,
)


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


class VerificationStore:
    def __init__(self, db: DB):
        self.db = db

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

    def admit(self, request: VerificationAdmission) -> AdmissionCommit:
        now_text = now_iso()

        def _tx(conn) -> AdmissionCommit:
            cursor = conn.execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,source_commit,kind,status,fail_reason,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                [
                    request.verification_id,
                    int(request.problem_id),
                    (
                        None
                        if request.workspace_id is None
                        else int(request.workspace_id)
                    ),
                    request.signature,
                    request.source_commit,
                    request.kind or Kind.ALL.value,
                    Status.QUEUED.value,
                    "",
                    now_text,
                    None,
                ],
            )
            outcome = "admitted" if int(cursor.rowcount or 0) == 1 else "already-exists"
            return AdmissionCommit(
                verification_id=request.verification_id,
                outcome=outcome,
            )

        return self.db.write_transaction(_tx)

    def list_visible_rows(
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
            WHERE problem_id=?
              AND (workspace_id=? OR workspace_id IS NULL)
              AND kind IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [int(problem_id), int(workspace_id), *kind_tokens, max(1, int(limit))],
        )
        return [self._record_row(dict(row)) for row in rows]

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

    def visible_verification_rows(
        self,
        problem_id: int,
        workspace_id: int,
        *,
        limit: int,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
    ) -> list[WorkspaceVerificationRow]:
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.CUSTOM.value]
        placeholders = ",".join(("?" for _ in kind_tokens))
        rows = self.db.fetch_all(
            f"""
            SELECT id,status,signature,source_commit,kind,fail_reason,error,
                   sanity_status,created_at,finished_at
            FROM verifications
            WHERE problem_id=?
              AND (workspace_id=? OR workspace_id IS NULL)
              AND kind IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [problem_id, workspace_id, *kind_tokens, max(1, int(limit))],
        )
        return [self._workspace_row(row) for row in rows]

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
        return [self._workspace_row(row) for row in rows]

    @staticmethod
    def _workspace_row(row: Mapping[str, object]) -> WorkspaceVerificationRow:
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

    def visible_verification_rows_many(
        self,
        subjects: list[WorkspaceVerificationKey],
        *,
        limit: int,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
    ) -> dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]]:
        keys = list(
            dict.fromkeys(
                (int(problem_id), int(workspace_id))
                for problem_id, workspace_id in subjects
            )
        )
        if not keys:
            return {}
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.CUSTOM.value]
        requested_values = ",".join("(?,?)" for _key in keys)
        kind_placeholders = ",".join("?" for _kind in kind_tokens)
        rows = self.db.fetch_all(
            f"""
            WITH requested(problem_id,workspace_id) AS (
                VALUES {requested_values}
            ), ranked AS (
                SELECT v.id,r.problem_id,
                       r.workspace_id AS requested_workspace_id,
                       v.status,v.signature,v.source_commit,v.kind,
                       v.fail_reason,v.error,v.sanity_status,
                       v.created_at,v.finished_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY r.problem_id,r.workspace_id
                           ORDER BY v.created_at DESC
                       ) AS row_number
                FROM requested r
                JOIN verifications v
                  ON v.problem_id=r.problem_id
                 AND (v.workspace_id=r.workspace_id OR v.workspace_id IS NULL)
                WHERE v.kind IN ({kind_placeholders})
            )
            SELECT * FROM ranked
            WHERE row_number<=?
            ORDER BY problem_id,requested_workspace_id,created_at DESC
            """,
            [
                *(value for key in keys for value in key),
                *kind_tokens,
                max(1, int(limit)),
            ],
        )
        result: dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]] = {
            key: [] for key in keys
        }
        for row in rows:
            key = (int(row["problem_id"]), int(row["requested_workspace_id"]))
            result[key].append(self._workspace_row(row))
        return result

    def workspace_verification_rows_many(
        self,
        subjects: list[WorkspaceVerificationKey],
        *,
        limit: int,
        kinds: tuple[str, ...] = (Kind.ALL.value, Kind.CUSTOM.value),
    ) -> dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]]:
        keys = list(
            dict.fromkeys(
                (int(problem_id), int(workspace_id))
                for problem_id, workspace_id in subjects
            )
        )
        if not keys:
            return {}
        kind_tokens = list(kinds) or [Kind.ALL.value, Kind.CUSTOM.value]
        requested_values = ",".join("(?,?)" for _key in keys)
        kind_placeholders = ",".join("?" for _kind in kind_tokens)
        rows = self.db.fetch_all(
            f"""
            WITH requested(problem_id,workspace_id) AS (
                VALUES {requested_values}
            ), ranked AS (
                SELECT v.id,v.problem_id,v.workspace_id,v.status,v.signature,
                       v.source_commit,v.kind,v.fail_reason,v.error,
                       v.sanity_status,v.created_at,v.finished_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY v.problem_id,v.workspace_id
                           ORDER BY v.created_at DESC
                       ) AS row_number
                FROM verifications v
                JOIN requested r
                  ON r.problem_id=v.problem_id
                 AND r.workspace_id=v.workspace_id
                WHERE v.kind IN ({kind_placeholders})
            )
            SELECT * FROM ranked
            WHERE row_number<=?
            ORDER BY problem_id,workspace_id,created_at DESC
            """,
            [
                *(value for key in keys for value in key),
                *kind_tokens,
                max(1, int(limit)),
            ],
        )
        result: dict[WorkspaceVerificationKey, list[WorkspaceVerificationRow]] = {
            key: [] for key in keys
        }
        for row in rows:
            key = (int(row["problem_id"]), int(row["workspace_id"]))
            result[key].append(
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
            )
        return result

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
