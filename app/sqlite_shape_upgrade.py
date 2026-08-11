"""Concrete SQLite table-shape upgrades required by the current schema."""

from __future__ import annotations

import sqlite3


class IncompatibleSchemaError(RuntimeError):
    """Raised when an existing database cannot satisfy the current schema."""


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> dict[str, bool]:
    return {
        str(row[1]): bool(row[3])
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        [table_name],
    ).fetchone()
    return row is not None


def _create_current_table(
    connection: sqlite3.Connection,
    statement: str,
) -> None:
    connection.execute(statement)


def _exports_need_rebuild(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "exports"):
        return False
    columns = _table_columns(connection, "exports")
    if "options_hash" not in columns:
        return False
    expected_old_columns = {
        "id",
        "problem_id",
        "materialization_id",
        "export_type",
        "options_hash",
        "filename",
        "archive_rel_path",
        "sha256",
        "size_bytes",
        "source_commit",
        "created_at",
    }
    if not expected_old_columns.issubset(columns):
        raise IncompatibleSchemaError(
            "exports options-hash table is missing required columns"
        )
    if not _table_exists(connection, "export_jobs"):
        raise IncompatibleSchemaError(
            "exports options-hash upgrade requires export_jobs"
        )
    return True


def _contest_items_need_rebuild(connection: sqlite3.Connection) -> bool:
    if not _table_exists(connection, "contest_build_items"):
        return False
    columns = _table_columns(connection, "contest_build_items")
    current = (
        columns.get("materialization_id"),
        columns.get("archive_sha256"),
    )
    if current == (False, False):
        return False
    if current != (True, True):
        raise IncompatibleSchemaError(
            "contest_build_items materialization columns have inconsistent nullability"
        )
    return True


def _rebuild_exports(
    connection: sqlite3.Connection,
    create_statement: str,
) -> None:
    connection.execute(
        "UPDATE export_jobs SET export_id=NULL WHERE export_id IS NOT NULL"
    )
    connection.execute("DROP TABLE exports")
    _create_current_table(connection, create_statement)


def _rebuild_contest_items(
    connection: sqlite3.Connection,
    create_statement: str,
) -> None:
    connection.execute(
        "ALTER TABLE contest_build_items RENAME TO contest_build_items_not_nullable"
    )
    _create_current_table(connection, create_statement)
    connection.execute(
        """
        INSERT INTO contest_build_items(
            id,job_id,contest_problem_id,position,label,problem_id,
            statement_folder,source_commit,revision_number,
            materialization_id,archive_sha256
        )
        SELECT
            id,job_id,contest_problem_id,position,label,problem_id,
            statement_folder,source_commit,revision_number,
            materialization_id,archive_sha256
        FROM contest_build_items_not_nullable
        """
    )
    connection.execute("DROP TABLE contest_build_items_not_nullable")


def apply_shape_upgrades(
    connection: sqlite3.Connection,
    *,
    exports_create_statement: str,
    contest_items_create_statement: str,
) -> None:
    """Apply the two concrete historical table reconstructions atomically."""

    rebuild_exports = _exports_need_rebuild(connection)
    rebuild_contest_items = _contest_items_need_rebuild(connection)
    if not rebuild_exports and not rebuild_contest_items:
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if rebuild_exports:
            _rebuild_exports(connection, exports_create_statement)
        if rebuild_contest_items:
            _rebuild_contest_items(connection, contest_items_create_statement)
        violations = connection.execute("PRAGMA foreign_key_check").fetchmany(10)
        if violations:
            details = [tuple(row) for row in violations]
            raise IncompatibleSchemaError(
                f"SQLite shape upgrade produced foreign-key violations: {details!r}"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise RuntimeError("could not restore SQLite foreign-key enforcement")


def _unique_column_sets(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for index in connection.execute(f"PRAGMA index_list({table_name})").fetchall():
        if not bool(index[2]):
            continue
        index_name = str(index[1]).replace('"', '""')
        columns = tuple(
            str(row[2])
            for row in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        )
        result.add(columns)
    return result


def validate_current_shape_constraints(connection: sqlite3.Connection) -> None:
    """Validate constraints whose shape is part of a concrete upgrade."""

    export_columns = _table_columns(connection, "exports")
    if "options_hash" in export_columns:
        raise IncompatibleSchemaError("exports still contains options_hash")
    export_identity = ("materialization_id", "export_type")
    if export_identity not in _unique_column_sets(connection, "exports"):
        raise IncompatibleSchemaError(
            "exports is missing its materialization/type unique constraint"
        )

    contest_columns = _table_columns(connection, "contest_build_items")
    if (
        contest_columns.get("materialization_id"),
        contest_columns.get("archive_sha256"),
    ) != (False, False):
        raise IncompatibleSchemaError(
            "contest_build_items materialization columns must be nullable"
        )
