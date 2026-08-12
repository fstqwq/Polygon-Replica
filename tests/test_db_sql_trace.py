import json
from unittest.mock import patch

from app.db import DB, now_iso, sqlite3
from tests.db_fixture import DBTestBase


class TestDBSqlTrace(DBTestBase):
    @staticmethod
    def _fetch_one(db: DB, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row | None:
        with db.conn() as conn:
            cursor = conn.execute(sql, params)
            row = cursor.fetchone()
        return row

    @staticmethod
    def _execute(db: DB, sql: str, params: tuple[object, ...] = ()) -> None:
        with db.conn() as conn:
            conn.execute(sql, params)
            conn.commit()

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
        with patch("app.db.logger.info") as info:
            row = self._fetch_one(self.db, "SELECT 1 AS value")
        self.assertIsNotNone(row)
        self.assertEqual(int(row["value"] or 0), 1)
        info.assert_not_called()

    def test_db_trace_can_be_enabled_at_runtime(self) -> None:
        values = dict(self.config_values.snapshot())
        values["DB_SQL_TRACE_ENABLED"] = True
        self.config_values.replace(values)
        with patch("app.db.logger.info") as info:
            row = self._fetch_one(self.db, "SELECT 1 AS value")
        self.assertIsNotNone(row)
        sql_texts = self._trace_sql_texts(info)
        self.assertTrue(any(text == "SELECT 1 AS value" for text in sql_texts), sql_texts)

    def test_db_trace_redacts_value_json_sql(self) -> None:
        values = dict(self.config_values.snapshot())
        values["DB_SQL_TRACE_ENABLED"] = True
        self.config_values.replace(values)
        payload = {"kind": "verification.start", "blob": "Y" * 1024}
        with patch("app.db.logger.info") as info:
            self._execute(
                self.db,
                """
                INSERT INTO system_config(key,value_json,updated_at,updated_by_user_id)
                VALUES(?,?,?,?)
                """,
                ["DB_SQL_TRACE_ENABLED", json.dumps(payload), now_iso(), None],
            )
        sql_texts = self._trace_sql_texts(info)
        value_text = next((text for text in sql_texts if "json_fields=value_json" in text), "")
        self.assertTrue(value_text, sql_texts)
        self.assertNotIn('"blob": "', value_text)
