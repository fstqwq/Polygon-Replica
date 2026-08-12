"""SQLite mechanics for resetting and compacting derived state."""

from pathlib import Path

from app.db import (
    DB,
    current_index_statements_for_tables,
    current_schema_statements_for_tables,
)


class ArtifactCleanupDatabase:
    def __init__(self, db: DB, database_path: Path) -> None:
        self._db = db
        self._database_path = database_path

    def table_counts(self, tables: tuple[str, ...]) -> dict[str, int]:
        expressions = [
            f"(SELECT COUNT(*) FROM {table}) AS {table}" for table in tables
        ]
        row = self._db.fetch_one("SELECT " + ", ".join(expressions))
        if row is None:
            raise RuntimeError("artifact usage query returned no row")
        return {table: int(row[table]) for table in tables}

    def reset_tables(self, tables: tuple[str, ...]) -> dict[str, int]:
        def transaction(connection) -> dict[str, int]:
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tables
            }
            for table in tables:
                connection.execute(f"DROP TABLE {table}")
            for statement in current_schema_statements_for_tables(tables):
                connection.execute(statement)
            for statement in current_index_statements_for_tables(tables):
                connection.execute(statement)
            return counts

        return self._db.write_schema_reset_transaction(transaction)

    def storage_bytes(self) -> int:
        paths = (
            self._database_path,
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        )
        total = 0
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink():
                    total += int(path.stat().st_size)
            except OSError:
                continue
        return total

    def checkpoint_truncate(self) -> None:
        with self._db.conn() as connection:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) != 0:
            raise RuntimeError(f"SQLite WAL checkpoint remained busy: {row!r}")

    def vacuum(self) -> None:
        self.checkpoint_truncate()
        with self._db.conn() as connection:
            connection.execute("VACUUM")
        self.checkpoint_truncate()
