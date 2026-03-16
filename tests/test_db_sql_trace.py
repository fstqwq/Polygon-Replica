from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.db import DB, now_iso
from .common import testsuite_root


class _TraceValues:
    def __init__(self, enabled: bool) -> None:
        self.DB_SQL_TRACE_ENABLED = bool(enabled)


class TestDBSqlTrace(TestCase):
    def _make_db(self) -> DB:
        root = testsuite_root() / f"db-trace-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=True)
        return DB(root / "trace.db")

    @staticmethod
    def _trace_sql_texts(info_mock) -> list[str]:
        rows: list[str] = []
        for call in info_mock.call_args_list:
            if not call.args:
                continue
            if str(call.args[0] or "").strip() != "db.sql pid=%s tid=%s conn=%s sql=%s":
                continue
            if len(call.args) >= 5:
                rows.append(str(call.args[4] or ""))
        return rows

    def test_db_trace_is_disabled_by_default(self) -> None:
        db = self._make_db()
        db.init()
        with patch("app.db.logger.info") as info:
            row = db.fetch_one("SELECT 1 AS value")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["value"] or 0), 1)
        info.assert_not_called()

    def test_db_trace_can_be_enabled_at_runtime(self) -> None:
        db = self._make_db()
        db.init()
        db.apply_runtime_values(_TraceValues(True))
        with patch("app.db.logger.info") as info:
            row = db.fetch_one("SELECT 1 AS value")
        self.assertIsNotNone(row)
        sql_texts = self._trace_sql_texts(info)
        self.assertTrue(any(text == "SELECT 1 AS value" for text in sql_texts), sql_texts)

    def test_db_trace_redacts_summary_json_sql(self) -> None:
        db = self._make_db()
        db.init()
        db.apply_runtime_values(_TraceValues(True))
        problem_slug = f"alice/{uuid.uuid4().hex[:8]}"
        summary_payload = {
            "tests": [{"test": "001.in", "feedback": "line one\nline two"}],
            "blob": "X" * 2048,
        }
        db.execute(
            "INSERT INTO problems(slug,name,repo_name,created_at) VALUES(?,?,?,?)",
            [problem_slug, "Sample", "sample", now_iso()],
        )
        problem_row = db.fetch_one("SELECT id FROM problems WHERE slug=?", [problem_slug])
        self.assertIsNotNone(problem_row)
        db.execute(
            """
            INSERT INTO verifications(id,problem_id,source_commit,source_ref,kind,status,summary_json,artifact_path,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                "ver-trace",
                int(problem_row["id"] or 0),
                "",
                "main",
                "build",
                "ok",
                json.dumps({"seed": True}),
                str(Path("/tmp/verification")),
                now_iso(),
            ],
        )
        with patch("app.db.logger.info") as info:
            db.execute(
                "UPDATE verifications SET summary_json=? WHERE id=?",
                [json.dumps(summary_payload), "ver-trace"],
            )
        sql_texts = self._trace_sql_texts(info)
        trace_text = next((text for text in sql_texts if "json_fields=summary_json" in text), "")
        self.assertTrue(trace_text, sql_texts)
        self.assertIn("UPDATE verifications", trace_text)
        self.assertIn("<redacted-json>", trace_text)
        self.assertNotIn('"blob": "', trace_text)
        self.assertNotIn("line one", trace_text)

    def test_db_trace_redacts_details_and_value_json_sql(self) -> None:
        db = self._make_db()
        db.init()
        db.apply_runtime_values(_TraceValues(True))
        payload = {"kind": "verification.start", "blob": "Y" * 1024}
        with patch("app.db.logger.info") as info:
            db.execute(
                """
                INSERT INTO audit_log(actor_user_id,problem_id,action,details_json,created_at)
                VALUES(?,?,?,?,?)
                """,
                [None, None, "verification.start", json.dumps(payload), now_iso()],
            )
            db.execute(
                """
                INSERT INTO system_config(key,value_json,updated_at,updated_by_user_id)
                VALUES(?,?,?,?)
                """,
                ["DB_SQL_TRACE_ENABLED", json.dumps(payload), now_iso(), None],
            )
        sql_texts = self._trace_sql_texts(info)
        details_text = next((text for text in sql_texts if "json_fields=details_json" in text), "")
        value_text = next((text for text in sql_texts if "json_fields=value_json" in text), "")
        self.assertTrue(details_text, sql_texts)
        self.assertTrue(value_text, sql_texts)
        self.assertNotIn('"blob": "', details_text)
        self.assertNotIn('"blob": "', value_text)
