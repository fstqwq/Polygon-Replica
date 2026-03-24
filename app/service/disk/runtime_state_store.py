from __future__ import annotations

import json
from pathlib import Path

from app.db import DB


class RuntimeStateStore:
    _ALLOWED_SUMMARY_TABLES = {"previews", "verifications", "contest_jobs"}

    def __init__(self, db: DB):
        self.db = db

    def cancel_inflight_summary_rows(self, table_name: str, reason: str, *, now_text: str) -> list[str]:
        safe_table = table_name.strip()
        if safe_table not in self._ALLOWED_SUMMARY_TABLES:
            return []
        if safe_table == "verifications":
            return self._cancel_inflight_verifications(reason, now_text=now_text)
        if safe_table == "previews":
            return self._cancel_inflight_previews(reason, now_text=now_text)
        try:
            rows = self.db.fetch_all(f"SELECT id FROM {safe_table} WHERE status IN ('running','queued','pending')")
        except Exception as exc:
            return [f"startup {safe_table} inflight scan failed: {exc}"]
        warnings: list[str] = []
        for row in rows:
            row_id = str(row["id"] or "").strip()
            if not row_id:
                continue
            try:
                self.db.execute(
                    f"""
                    UPDATE {safe_table}
                    SET status='failed', finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    [now_text, row_id],
                )
            except Exception as exc:
                warnings.append(f"startup {safe_table} inflight cancel failed for {row_id}: {exc}")
        return warnings

    def _cancel_inflight_previews(self, reason: str, *, now_text: str) -> list[str]:
        rows = self.db.fetch_all(
            """
            SELECT id
            FROM previews
            WHERE status IN ('running','queued','pending')
            """
        )
        warnings: list[str] = []
        for row in rows:
            preview_id = str(row["id"] or "")
            if not preview_id:
                continue
            try:
                artifact_row = self.db.fetch_one("SELECT artifact_path FROM previews WHERE id=?", [preview_id])
                artifact_path = "" if artifact_row is None else str(artifact_row["artifact_path"] or "")
                if artifact_path:
                    summary_path = (Path(artifact_path).resolve() / "summary.json").resolve()
                else:
                    summary_path = None
            except Exception:
                summary_path = None
            if summary_path is not None:
                try:
                    summary_path.parent.mkdir(parents=True, exist_ok=True)
                    summary_path.write_text(
                        json.dumps(
                            {
                                "cancelled": True,
                                "cancel_reason": reason,
                                "status": "failed",
                                "finished_at": now_text,
                                "error": reason,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            try:
                self.db.execute(
                    """
                    UPDATE previews
                    SET status='failed', finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    [now_text, preview_id],
                )
            except Exception as exc:
                warnings.append(f"startup previews inflight cancel failed for {preview_id}: {exc}")
        return warnings

    def _cancel_inflight_verifications(self, reason: str, *, now_text: str) -> list[str]:
        rows = self.db.fetch_all(
            """
            SELECT id,status
            FROM verifications
            WHERE status IN ('running','queued','pending')
            """
        )
        warnings: list[str] = []
        for row in rows:
            verification_id = str(row["id"] or "")
            if not verification_id:
                continue
            try:
                self.db.execute(
                    """
                    UPDATE verifications
                    SET status='cancelled', fail_reason=?, finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    [reason, now_text, verification_id],
                )
            except Exception as exc:
                warnings.append(f"startup verifications inflight cancel failed for {verification_id}: {exc}")
        return warnings
