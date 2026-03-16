from __future__ import annotations

from typing import Callable

from .common import db


def db_fetch_one(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.fetch_one(sql, values)


def db_fetch_all(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.fetch_all(sql, values)


def db_execute(sql: str, params: list[object] | tuple[object, ...] | None = None):
    values = [] if params is None else list(params)
    return db.execute(sql, values)


def db_write_transaction(func: Callable):
    return db.write_transaction(func)


def db_connection():
    return db.conn()


def judgehost_fetch_case(service, case_id: int):
    return service._judgehost_state_store.fetch_case(int(case_id))


def judgehost_fetch_job(service, job_id: int):
    return service._judgehost_state_store.fetch_job(int(job_id))


def judgehost_cases_for_run(service, run_id: str):
    return service._judgehost_state_store.cases_for_run(run_id)
