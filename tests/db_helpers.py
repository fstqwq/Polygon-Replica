from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .common import db
from app.impl.runtime.config import config


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


def read_verification_summary(verification_id: str) -> dict[str, object]:
    return dict(config.verification_service.verification_metadata(str(verification_id).strip()))


def write_verification_summary(verification_id: str, summary: dict[str, object]) -> None:
    config.verification_service.persist_verification_metadata(str(verification_id).strip(), dict(summary))


def read_preview_summary(preview_id: str) -> dict[str, object]:
    row = db_fetch_one("SELECT artifact_path FROM previews WHERE id=?", [str(preview_id).strip()])
    if row is None:
        return {}
    artifact_path = str(row["artifact_path"] or "")
    if not artifact_path:
        return {}
    try:
        text = (Path(artifact_path).resolve() / "summary.json").read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_preview_summary(preview_id: str, summary: dict[str, object]) -> None:
    row = db_fetch_one("SELECT artifact_path FROM previews WHERE id=?", [str(preview_id).strip()])
    if row is None:
        raise AssertionError(f"preview row missing: {preview_id}")
    artifact_path = str(row["artifact_path"] or "")
    if not artifact_path:
        raise AssertionError(f"preview artifact path missing: {preview_id}")
    path = (Path(artifact_path).resolve() / "summary.json").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )


def read_contest_job_summary(contest_id: int, job_id: str) -> dict[str, object]:
    contest_row = db_fetch_one("SELECT slug FROM contests WHERE id=?", [int(contest_id)])
    if contest_row is None:
        return {}
    path = config.contest_service.job_root(str(contest_row["slug"]), str(job_id).strip()) / "summary.json"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_contest_job_summary(contest_id: int, job_id: str, summary: dict[str, object]) -> None:
    contest_row = db_fetch_one("SELECT slug FROM contests WHERE id=?", [int(contest_id)])
    if contest_row is None:
        raise AssertionError(f"contest missing: {contest_id}")
    path = config.contest_service.job_root(str(contest_row["slug"]), str(job_id).strip()) / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
