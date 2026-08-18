"""SQLite metadata for disposable HTML and PDF statement previews."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal, TypedDict, cast

from app.db import DB, now_iso


StatementPreviewSubject = Literal["problem", "contest"]
StatementPreviewSource = Literal["workspace", "native_package"]
StatementPreviewOutput = Literal["html", "pdf"]


class StatementPreviewRow(TypedDict):
    id: str
    actor_user_id: int
    subject_kind: StatementPreviewSubject
    problem_id: int | None
    contest_id: int | None
    source_kind: StatementPreviewSource
    output_kind: StatementPreviewOutput
    language: str
    input_identity: str
    options: dict[str, object]
    status: str
    summary: dict[str, object]
    created_at: str
    finished_at: str


class StatementPreviewStore:
    def __init__(self, db: DB) -> None:
        self._db = db

    def insert(
        self,
        *,
        preview_id: str,
        actor_user_id: int,
        subject_kind: StatementPreviewSubject,
        problem_id: int | None,
        contest_id: int | None,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
        options: dict[str, object],
    ) -> None:
        self._db.execute(
            """
            INSERT INTO statement_previews(
                id,actor_user_id,subject_kind,problem_id,contest_id,source_kind,output_kind,
                language,input_identity,options_json,status,summary_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                preview_id,
                actor_user_id,
                subject_kind,
                problem_id,
                contest_id,
                source_kind,
                output_kind,
                language,
                input_identity,
                self._encode(options),
                "running",
                "{}",
                now_iso(),
            ],
        )

    def finish(self, preview_id: str, *, status: str, summary: dict[str, object]) -> None:
        self._db.execute(
            """
            UPDATE statement_previews
            SET status=?,summary_json=?,finished_at=?
            WHERE id=?
            """,
            [status, self._encode(summary), now_iso(), preview_id],
        )

    def row(
        self,
        preview_id: str,
        *,
        actor_user_id: int | None = None,
    ) -> StatementPreviewRow | None:
        where = "id=?" if actor_user_id is None else "id=? AND actor_user_id=?"
        params = [preview_id] if actor_user_id is None else [preview_id, actor_user_id]
        row = self._db.fetch_one(
            f"SELECT * FROM statement_previews WHERE {where}",
            params,
        )
        return self._project(row) if row is not None else None

    def latest_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
    ) -> StatementPreviewRow | None:
        row = self._db.fetch_one(
            """
            SELECT * FROM statement_previews
            WHERE problem_id=? AND actor_user_id=?
              AND source_kind=? AND output_kind=? AND language=?
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            [problem_id, actor_user_id, source_kind, output_kind, language],
        )
        return self._project(row) if row is not None else None

    def latest_contest(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
    ) -> StatementPreviewRow | None:
        row = self._db.fetch_one(
            """
            SELECT * FROM statement_previews
            WHERE contest_id=? AND actor_user_id=?
              AND source_kind=? AND output_kind=? AND language=?
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            [contest_id, actor_user_id, source_kind, output_kind, language],
        )
        return self._project(row) if row is not None else None

    def cached_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
    ) -> StatementPreviewRow | None:
        row = self._db.fetch_one(
            """
            SELECT * FROM statement_previews
            WHERE problem_id=? AND actor_user_id=?
              AND source_kind=? AND output_kind=? AND language=?
              AND input_identity=? AND status='ok'
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            [
                problem_id,
                actor_user_id,
                source_kind,
                output_kind,
                language,
                input_identity,
            ],
        )
        return self._project(row) if row is not None else None

    def cached_contest(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
        options: dict[str, object],
    ) -> StatementPreviewRow | None:
        row = self._db.fetch_one(
            """
            SELECT * FROM statement_previews
            WHERE contest_id=? AND actor_user_id=?
              AND source_kind=? AND output_kind=? AND language=?
              AND input_identity=? AND options_json=? AND status='ok'
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            [
                contest_id,
                actor_user_id,
                source_kind,
                output_kind,
                language,
                input_identity,
                self._encode(options),
            ],
        )
        return self._project(row) if row is not None else None

    @classmethod
    def _project(cls, row: sqlite3.Row) -> StatementPreviewRow:
        return {
            "id": str(row["id"]),
            "actor_user_id": int(row["actor_user_id"]),
            "subject_kind": cast(StatementPreviewSubject, str(row["subject_kind"])),
            "problem_id": int(row["problem_id"]) if row["problem_id"] is not None else None,
            "contest_id": int(row["contest_id"]) if row["contest_id"] is not None else None,
            "source_kind": cast(StatementPreviewSource, str(row["source_kind"])),
            "output_kind": cast(StatementPreviewOutput, str(row["output_kind"])),
            "language": str(row["language"]),
            "input_identity": str(row["input_identity"]),
            "options": cls._decode(row["options_json"]),
            "status": str(row["status"]),
            "summary": cls._decode(row["summary_json"]),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"] or ""),
        }

    @staticmethod
    def _encode(value: dict[str, object]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(raw: object) -> dict[str, object]:
        try:
            value = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
