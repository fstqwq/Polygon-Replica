from __future__ import annotations

import json

from app.db import DB


class RuntimeStateStore:
    _ALLOWED_SUMMARY_TABLES = {"previews", "verifications", "contest_jobs"}

    def __init__(self, db: DB):
        self.db = db

    def cancel_inflight_summary_rows(self, table_name: str, reason: str, *, now_text: str) -> list[str]:
        safe_table = table_name.strip()
        if safe_table not in self._ALLOWED_SUMMARY_TABLES:
            return []
        try:
            rows = self.db.fetch_all(
                f"SELECT id,summary_json FROM {safe_table} WHERE status IN ('running','queued','pending')"
            )
        except Exception as exc:
            return [f"startup {safe_table} inflight scan failed: {exc}"]
        warnings: list[str] = []
        for row in rows:
            row_id = str(row["id"] or "").strip()
            if not row_id:
                continue
            summary_obj: dict[str, object] = {}
            try:
                parsed = json.loads(str(row["summary_json"] or "").strip() or "{}")
                if isinstance(parsed, dict):
                    summary_obj = dict(parsed)
            except Exception:
                summary_obj = {}
            summary_obj["cancelled"] = True
            summary_obj["cancel_reason"] = reason
            summary_obj["status"] = "failed"
            summary_obj["finished_at"] = now_text
            if not summary_obj.get("error"):
                summary_obj["error"] = reason
            try:
                self.db.execute(
                    f"""
                    UPDATE {safe_table}
                    SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                    WHERE id=?
                    """,
                    [json.dumps(summary_obj), now_text, row_id],
                )
            except Exception as exc:
                warnings.append(f"startup {safe_table} inflight cancel failed for {row_id}: {exc}")
        return warnings
