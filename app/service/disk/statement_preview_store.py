"""SQLite metadata for disposable HTML and PDF statement previews."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from app.db import DB, SQLValue, now_iso
from app.service.statement.preview_state import (
    ContestStatementPreviewItem,
    StatementPdfProblemResult,
    StatementPdfTotals,
    StatementPreviewOutput,
    StatementPreviewRow,
    StatementPreviewSource,
    StatementPreviewSubject,
    StatementPreviewSummary,
)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("preview summary text must be a string")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("preview summary count must be an integer")
    return value


def _text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("preview summary text list is invalid")
    return [_text(item) for item in value]


def _items(value: object) -> list[ContestStatementPreviewItem]:
    if not isinstance(value, list):
        raise ValueError("preview items must be a list")
    items: list[ContestStatementPreviewItem] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("preview item must be an object")
        items.append({
            "idx": _text(item.get("idx", "")),
            "problem_id": _integer(item.get("problem_id", 0)),
            "problem_slug": _text(item.get("problem_slug", "")),
            "preview_id": _text(item.get("preview_id", "")),
            "status": _text(item.get("status", "failed")),
            "error": _text(item.get("error", "")),
        })
    return items


def _pdf_results(value: object) -> list[StatementPdfProblemResult]:
    if not isinstance(value, list):
        raise ValueError("preview PDF results must be a list")
    results: list[StatementPdfProblemResult] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("preview PDF result must be an object")
        result: StatementPdfProblemResult = {
            "idx": _text(item.get("idx", "")),
            "problem_id": _integer(item.get("problem_id", 0)),
            "problem_slug": _text(item.get("problem_slug", "")),
            "source_folder": _text(item.get("source_folder", "")),
            "status": _text(item.get("status", "failed")),
            "error": _text(item.get("error", "")),
        }
        if "preamble_lines" in item:
            result["preamble_lines"] = _text_list(item["preamble_lines"])
        results.append(result)
    return results


def _pdf_totals(value: object) -> StatementPdfTotals:
    if not isinstance(value, dict):
        raise ValueError("preview PDF totals must be an object")
    return {
        "total": _integer(value.get("total", 0)),
        "success": _integer(value.get("success", 0)),
        "failed": _integer(value.get("failed", 0)),
    }


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
                "{}",
                "running",
                "{}",
                now_iso(),
            ],
        )

    def finish(self, preview_id: str, *, status: str, summary: StatementPreviewSummary) -> None:
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
        params: list[SQLValue] = [preview_id]
        if actor_user_id is not None:
            params.append(actor_user_id)
        row = self._db.fetch_one(
            f"SELECT * FROM statement_previews WHERE {where}",
            params,
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
    ) -> StatementPreviewRow | None:
        row = self._db.fetch_one(
            """
            SELECT * FROM statement_previews
            WHERE contest_id=? AND actor_user_id=?
              AND source_kind=? AND output_kind=? AND language=?
              AND input_identity=? AND options_json='{}' AND status='ok'
            ORDER BY created_at DESC,id DESC LIMIT 1
            """,
            [
                contest_id,
                actor_user_id,
                source_kind,
                output_kind,
                language,
                input_identity,
            ],
        )
        return self._project(row) if row is not None else None

    @classmethod
    def _project(cls, row: sqlite3.Row) -> StatementPreviewRow:
        subject_kind = row["subject_kind"]
        source_kind = row["source_kind"]
        output_kind = row["output_kind"]
        if subject_kind not in {"problem", "contest"}:
            raise ValueError("invalid statement preview subject")
        if source_kind not in {"workspace", "native_package"}:
            raise ValueError("invalid statement preview source")
        if output_kind not in {"html", "pdf"}:
            raise ValueError("invalid statement preview output")
        return {
            "id": str(row["id"]),
            "actor_user_id": cls._required_int(row["actor_user_id"]),
            "subject_kind": "problem" if subject_kind == "problem" else "contest",
            "problem_id": cls._optional_int(row["problem_id"]),
            "contest_id": cls._optional_int(row["contest_id"]),
            "source_kind": "workspace" if source_kind == "workspace" else "native_package",
            "output_kind": "html" if output_kind == "html" else "pdf",
            "language": str(row["language"]),
            "input_identity": str(row["input_identity"]),
            "status": str(row["status"]),
            "summary": cls._decode(row["summary_json"]),
            "created_at": str(row["created_at"]),
            "finished_at": str(row["finished_at"] or ""),
        }

    @staticmethod
    def _encode(value: StatementPreviewSummary) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode(raw: SQLValue) -> StatementPreviewSummary:
        try:
            value = json.loads(str(raw or "{}"))
            if not isinstance(value, dict):
                return {}
            summary: StatementPreviewSummary = {}
            text_key: Literal["error", "content", "pdf", "job_type", "contest_slug", "language", "latex_log", "filename", "log"]
            for text_key in ("error", "content", "pdf", "job_type", "contest_slug", "language", "latex_log", "filename", "log"):
                if text_key in value:
                    summary[text_key] = _text(value[text_key])
            count_key: Literal["sample_count", "successful", "failed"]
            for count_key in ("sample_count", "successful", "failed"):
                if count_key in value:
                    summary[count_key] = _integer(value[count_key])
            if "returncode" in value:
                summary["returncode"] = None if value["returncode"] is None else _integer(value["returncode"])
            if "warnings" in value:
                summary["warnings"] = _text_list(value["warnings"])
            if "resources" in value:
                summary["resources"] = _text_list(value["resources"])
            if "items" in value:
                summary["items"] = _items(value["items"])
            if "results" in value:
                summary["results"] = _pdf_results(value["results"])
            if "totals" in value:
                summary["totals"] = _pdf_totals(value["totals"])
            return summary
        except ValueError:
            return {}

    @staticmethod
    def _required_int(raw: str | int | float | bytes | None) -> int:
        if raw is None:
            raise ValueError("required statement preview integer is null")
        return int(raw)

    @staticmethod
    def _optional_int(raw: str | int | float | bytes | None) -> int | None:
        return None if raw is None else int(raw)
