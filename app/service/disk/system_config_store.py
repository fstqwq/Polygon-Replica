import json
import sqlite3

from app.db import DB


class SystemConfigStore:
    def __init__(self, db: DB):
        self.db = db

    @staticmethod
    def set_override_in_transaction(
        conn: sqlite3.Connection,
        *,
        key: str,
        value: object,
        default: object,
        actor_user_id: int,
        updated_at: str,
    ) -> None:
        if value == default:
            conn.execute("DELETE FROM system_config WHERE key=?", [key])
            return
        conn.execute(
            """
            INSERT INTO system_config(key, value_json, updated_at, updated_by_user_id)
            VALUES(?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at,
                updated_by_user_id=excluded.updated_by_user_id
            """,
            [
                key,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                updated_at,
                int(actor_user_id),
            ],
        )

    def replace_overrides(
        self,
        *,
        keys: list[str],
        values: dict[str, object],
        defaults: dict[str, object],
        actor_user_id: int,
        updated_at: str,
    ) -> None:
        def _tx(conn) -> None:
            for key in keys:
                self.set_override_in_transaction(
                    conn,
                    key=key,
                    value=values[key],
                    default=defaults[key],
                    actor_user_id=actor_user_id,
                    updated_at=updated_at,
                )
            conn.execute(
                "DELETE FROM system_config WHERE key NOT IN ({})".format(
                    ",".join("?" for _ in keys)
                ),
                list(keys),
            )

        self.db.write_transaction(_tx)

    def clear_overrides(self) -> None:
        def _tx(conn) -> None:
            conn.execute("DELETE FROM system_config")

        self.db.write_transaction(_tx)

    def override_rows(self) -> list[dict[str, str]]:
        try:
            rows = self.db.fetch_all("SELECT key, value_json FROM system_config ORDER BY key ASC")
        except sqlite3.OperationalError as exc:
            if "no such table: system_config" in str(exc).lower():
                return []
            raise
        return [{"key": str(row["key"]), "value_json": str(row["value_json"])} for row in rows]
