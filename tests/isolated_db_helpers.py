"""Persistence boundary for tests that own a private SQLite database."""

import sqlite3
from contextlib import AbstractContextManager
from typing import Callable, TypeVar

from app.db import DB


_Result = TypeVar("_Result")


def isolated_db_fetch_one(
    database: DB,
    sql: str,
    params: list[object] | tuple[object, ...] | None = None,
) -> sqlite3.Row | None:
    values = [] if params is None else list(params)
    return database.fetch_one(sql, values)


def isolated_db_fetch_all(
    database: DB,
    sql: str,
    params: list[object] | tuple[object, ...] | None = None,
) -> list[sqlite3.Row]:
    values = [] if params is None else list(params)
    return database.fetch_all(sql, values)


def isolated_db_execute(
    database: DB,
    sql: str,
    params: list[object] | tuple[object, ...] | None = None,
) -> None:
    values = [] if params is None else list(params)
    database.execute(sql, values)


def isolated_db_write_transaction(
    database: DB,
    func: Callable[[sqlite3.Connection], _Result],
) -> _Result:
    return database.write_transaction(func)


def isolated_db_connection(
    database: DB,
) -> AbstractContextManager[sqlite3.Connection]:
    return database.conn()
