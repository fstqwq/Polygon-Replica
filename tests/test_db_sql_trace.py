import json

from app.db import now_iso
from tests.db_fixture import DBTestBase
from tests.isolated_db_helpers import isolated_db_execute, isolated_db_fetch_one


class TestDBSqlTrace(DBTestBase):
    def test_db_trace_follows_runtime_setting(self) -> None:
        with self.assertNoLogs("uvicorn.error", level="INFO"):
            row = isolated_db_fetch_one(self.db, "SELECT 1 AS value")
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], 1)

        values = dict(self.config_values.snapshot())
        values["DB_SQL_TRACE_ENABLED"] = True
        self.config_values.replace(values)
        with self.assertLogs("uvicorn.error", level="INFO") as emitted:
            row = isolated_db_fetch_one(self.db, "SELECT 2 AS value")
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], 2)
        self.assertTrue(any("sql=SELECT 2 AS value" in line for line in emitted.output))

        values["DB_SQL_TRACE_ENABLED"] = False
        self.config_values.replace(values)
        with self.assertNoLogs("uvicorn.error", level="INFO"):
            row = isolated_db_fetch_one(self.db, "SELECT 3 AS value")
        self.assertIsNotNone(row)
        self.assertEqual(row["value"], 3)

    def test_db_trace_redacts_value_json_sql(self) -> None:
        values = dict(self.config_values.snapshot())
        values["DB_SQL_TRACE_ENABLED"] = True
        self.config_values.replace(values)
        secret = "private-verification-payload"
        payload = {"kind": "verification.start", "blob": secret}
        with self.assertLogs("uvicorn.error", level="INFO") as emitted:
            isolated_db_execute(
                self.db,
                """
                INSERT INTO system_config(key,value_json,updated_at,updated_by_user_id)
                VALUES(?,?,?,?)
                """,
                ["DB_SQL_TRACE_ENABLED", json.dumps(payload), now_iso(), None],
            )
        output = "\n".join(emitted.output)
        self.assertIn("json_fields=value_json", output)
        self.assertNotIn(secret, output)
        row = isolated_db_fetch_one(self.db, "SELECT value_json FROM system_config WHERE key=?", ["DB_SQL_TRACE_ENABLED"])
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["value_json"]), payload)
