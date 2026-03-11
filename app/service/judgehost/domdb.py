from __future__ import annotations

from typing import Any, Callable


def is_domjudge_sql(sql: str) -> bool:
    token = str(sql or "").lower()
    return ("judgehost_domjudge_jobs" in token) or ("judgehost_domjudge_cases" in token)


def domjudge_active_job_for_host(
    hostname: str,
    *,
    fetch_all: Callable[[str, list[object] | tuple[object, ...] | None], list[Any]],
):
    rows = fetch_all(
        """
        SELECT j.*
        FROM judgehost_domjudge_jobs j
        WHERE j.lease_owner=? AND j.status IN ('leased','queued')
        ORDER BY j.job_id ASC
        LIMIT 1
        """,
        [hostname],
    )
    if not rows:
        return None
    return rows[0]


def domjudge_shared_pending_job(
    hostname: str,
    *,
    fetch_all: Callable[[str, list[object] | tuple[object, ...] | None], list[Any]],
):
    rows = fetch_all(
        """
        SELECT j.*
        FROM judgehost_domjudge_jobs j
        WHERE (
            (j.lease_owner=? AND j.status IN ('leased','queued'))
            OR ((j.lease_owner IS NULL OR TRIM(j.lease_owner)='') AND j.status='queued')
        )
          AND EXISTS (
            SELECT 1
            FROM judgehost_domjudge_cases c
            WHERE c.job_id=j.job_id AND c.status='pending'
          )
        ORDER BY
          CASE WHEN j.lease_owner=? THEN 0 ELSE 1 END,
          CASE WHEN j.status='leased' THEN 0 ELSE 1 END,
          j.created_at ASC,
          j.job_id ASC
        LIMIT 1
        """,
        [hostname, hostname],
    )
    if not rows:
        return None
    return rows[0]


def domjudge_cases_for_job(
    job_id: int,
    *,
    fetch_all: Callable[[str, list[object] | tuple[object, ...] | None], list[Any]],
    status: str | None = None,
) -> list[Any]:
    if status:
        return fetch_all(
            """
            SELECT *
            FROM judgehost_domjudge_cases
            WHERE job_id=? AND status=?
            ORDER BY ordinal ASC, id ASC
            """,
            [int(job_id), str(status)],
        )
    return fetch_all(
        """
        SELECT *
        FROM judgehost_domjudge_cases
        WHERE job_id=?
        ORDER BY ordinal ASC, id ASC
        """,
        [int(job_id)],
    )


