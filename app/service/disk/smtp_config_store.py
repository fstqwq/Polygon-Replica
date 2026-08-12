"""SQLite persistence for the system SMTP configuration."""

from dataclasses import dataclass

from app.db import DB, now_iso


@dataclass(frozen=True)
class StoredSmtpConfig:
    host: str
    port: int
    username: str
    password_ciphertext: str
    updated_at: str
    updated_by_user_id: int


class SmtpConfigStore:
    def __init__(self, db: DB):
        self.db = db

    def get(self) -> StoredSmtpConfig:
        row = self.db.fetch_one(
            """
            SELECT host, port, username, password_ciphertext, updated_at, updated_by_user_id
            FROM smtp_config
            WHERE id=1
            """
        )
        if row is None:
            return StoredSmtpConfig("", 587, "", "", "", 0)
        return StoredSmtpConfig(
            host=str(row["host"] or ""),
            port=int(row["port"] or 587),
            username=str(row["username"] or ""),
            password_ciphertext=str(row["password_ciphertext"] or ""),
            updated_at=str(row["updated_at"] or ""),
            updated_by_user_id=int(row["updated_by_user_id"] or 0),
        )

    def save(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password_ciphertext: str,
        actor_user_id: int,
    ) -> None:
        updated_at = now_iso()

        def _tx(conn) -> None:
            conn.execute(
                """
                INSERT INTO smtp_config(
                    id, host, port, username, password_ciphertext, updated_at, updated_by_user_id
                )
                VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    host=excluded.host,
                    port=excluded.port,
                    username=excluded.username,
                    password_ciphertext=excluded.password_ciphertext,
                    updated_at=excluded.updated_at,
                    updated_by_user_id=excluded.updated_by_user_id
                """,
                [host, int(port), username, password_ciphertext, updated_at, int(actor_user_id)],
            )

        self.db.write_transaction(_tx)
