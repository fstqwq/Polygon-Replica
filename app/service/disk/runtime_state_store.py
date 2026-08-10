from __future__ import annotations

import json
from app.db import DB
from app.service.platform.error_text import bounded_display_text


class RuntimeStateStore:
    _ALLOWED_SUMMARY_TABLES = {"previews", "contest_jobs"}

    def __init__(self, db: DB):
        self.db = db

    def cancel_inflight_summary_rows(self, table_name: str, reason: str, *, now_text: str) -> list[str]:
        safe_table = table_name.strip()
        if safe_table not in self._ALLOWED_SUMMARY_TABLES:
            return []
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
        safe_reason = bounded_display_text(reason)
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
                self.db.execute(
                    """
                    UPDATE previews
                    SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    [
                        json.dumps(
                            {
                                "cancelled": True,
                                "cancel_reason": safe_reason,
                                "status": "failed",
                                "finished_at": now_text,
                                "error": safe_reason,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                        now_text,
                        preview_id,
                    ],
                )
            except Exception as exc:
                warnings.append(f"startup previews inflight cancel failed for {preview_id}: {exc}")
        return warnings
