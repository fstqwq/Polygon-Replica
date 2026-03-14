from __future__ import annotations

from typing import Callable, Mapping


def domjudge_case_progress_for_runs(
    *,
    normalize_run_id: Callable[[str], str],
    db_fetch_all: Callable[[str, list[object]], list[object]],
    run_ids: list[str],
) -> dict[str, dict[str, int]]:
    safe_ids = [normalize_run_id(str(item or "").strip()) for item in list(run_ids or []) if str(item or "").strip()]
    if not safe_ids:
        return {}
    placeholders = ",".join(("?" for _ in safe_ids))
    rows = db_fetch_all(
        f"""
        SELECT j.run_id AS run_id,
               COUNT(c.id) AS total_cases,
               SUM(CASE WHEN c.status='reported' THEN 1 ELSE 0 END) AS reported_cases,
               SUM(CASE WHEN c.status='leased' THEN 1 ELSE 0 END) AS leased_cases
        FROM judgehost_domjudge_jobs j
        JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
        WHERE j.run_id IN ({placeholders})
        GROUP BY j.run_id
        """,
        [*safe_ids],
    )
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        run_id = str(row["run_id"] or "").strip()
        if not run_id:
            continue
        total = max(0, int(row["total_cases"] or 0))
        reported = max(0, int(row["reported_cases"] or 0))
        leased = max(0, int(row["leased_cases"] or 0))
        out[run_id] = {"total": total, "reported": min(total, reported) if total > 0 else reported, "leased": leased}
    return out


def domjudge_solve_main_progress(
    *,
    state_lock,
    tasks_by_id: Mapping[str, dict[str, object]],
    db_fetch_one: Callable[[str, list[object]], object | None],
    artifact_verification_id: str,
) -> dict[str, int]:
    safe_verification_id = str(artifact_verification_id or "").strip()
    if not safe_verification_id:
        return {"total": 0, "reported": 0}
    run_ids: list[str] = []
    with state_lock:
        for row in tasks_by_id.values():
            if str(row.get("artifact_verification_id") or "").strip() != safe_verification_id:
                continue
            run_id = str(row.get("run_id") or "").strip()
            if (not run_id) or (not run_id.startswith("r-solve-main-")):
                continue
            if run_id not in run_ids:
                run_ids.append(run_id)
    if not run_ids:
        return {"total": 0, "reported": 0}
    placeholders = ",".join(("?" for _ in run_ids))
    row = db_fetch_one(
        f"""
        SELECT COUNT(c.id) AS total_cases,
               SUM(CASE WHEN c.status='reported' THEN 1 ELSE 0 END) AS reported_cases
        FROM judgehost_domjudge_jobs j
        JOIN judgehost_domjudge_cases c ON c.job_id=j.job_id
        WHERE j.run_id IN ({placeholders})
        """,
        [*run_ids],
    )
    if row is None:
        return {"total": 0, "reported": 0}
    total = max(0, int(row["total_cases"] or 0))
    reported = max(0, int(row["reported_cases"] or 0))
    if total > 0 and reported > total:
        reported = total
    return {"total": total, "reported": reported}
