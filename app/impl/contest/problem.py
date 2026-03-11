from __future__ import annotations

import secrets
from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request

from app.db import now_iso
from app.impl.auth.public import template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import audit, form_text, workspace_access_context

from .common import (
    _contest_idx_label,
    _dedupe_preserve,
    _normalize_contest_problem_idx_required,
)
from .shared import (
    _contest_available_problem_rows,
    _contest_ctx,
    _contest_problem_rows,
    _contest_redirect,
    _create_contest_job,
    _load_contest_job,
    _next_contest_problem_idx,
    _problem_general_payload_map,
    _run_problem_general_update,
)

_C = config.constants
def contest_problems_page(request: Request, contest: str, user: str, q: str = "", job_id: str = ""):
    ctx = _contest_ctx(contest, user, "problems")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = _contest_problem_rows(contest_id, str(ctx["user"]["username"]), user_id)
    query = str(q or "").strip()
    available_rows = _contest_available_problem_rows(contest_id, user_id, query)
    latest_job = _load_contest_job(contest_id, job_id)
    return template_response(
        request,
        "contest_problems.html",
        {
            "ctx": ctx,
            "query": query,
            "problem_rows": rows,
            "available_rows": available_rows,
            "latest_job": latest_job,
        },
    )

def contest_problems_add(contest: str, user: str, problem_slugs: list[str] = Form([]), q: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    safe_slugs = _dedupe_preserve([form_text(item) for item in list(problem_slugs or [])])
    if not safe_slugs:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query=f"q={quote_plus(str(q or ''))}" if str(q or "").strip() else "", message="select at least one problem to add")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    added = 0
    failed: list[str] = []
    for slug in safe_slugs:
        problem_row = config.db.fetch_one("SELECT id,slug FROM problems WHERE slug=?", [slug])
        if problem_row is None:
            failed.append(f"{slug}: problem not found")
            continue
        problem_id = int(problem_row["id"])
        problem_access = workspace_access_context(problem_id, user_id)
        if not bool(problem_access.get("can_read")):
            failed.append(f"{slug}: no access to problem")
            continue
        existing = config.db.fetch_one(
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, problem_id],
        )
        if existing is not None:
            failed.append(f"{slug}: already in contest")
            continue
        try:
            idx = _next_contest_problem_idx(contest_id)
            config.db.execute(
                """
                INSERT INTO contest_problems(contest_id,idx,problem_id,added_by_user_id,created_at)
                VALUES(?,?,?,?,?)
                """,
                [contest_id, idx, problem_id, user_id, now_iso()],
            )
            added += 1
        except Exception as exc:
            failed.append(f"{slug}: {exc}")
    audit(
        int(ctx["user"]["id"]),
        None,
        "contest.problems.add",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "added_count": added,
            "failed_count": len(failed),
            "failed": failed[:20],
        },
    )
    msg = f"added {added} problem(s)"
    if failed:
        msg += f"; failed {len(failed)}"
    query_parts: list[str] = []
    if str(q or "").strip():
        query_parts.append(f"q={quote_plus(str(q or '').strip())}")
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query="&".join(query_parts), message=msg)

def contest_problems_remove(contest: str, user: str, problem_id: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    msg = "problem removed"
    try:
        pid = int(str(problem_id or "").strip())
    except Exception:
        pid = 0
    if pid <= 0:
        msg = "invalid problem id"
    else:
        config.db.execute(
            "DELETE FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message=msg)

def contest_problems_remove_selected(contest: str, user: str, selected_problem_ids: list[str] = Form([])):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    ids: list[int] = []
    for raw in list(selected_problem_ids or []):
        try:
            value = int(str(raw or "").strip())
        except Exception:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="select at least one problem to remove")
    contest_id = int(ctx["contest"]["id"])
    removed = 0
    for pid in ids:
        before = config.db.fetch_one(
            "SELECT id FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
        config.db.execute(
            "DELETE FROM contest_problems WHERE contest_id=? AND problem_id=?",
            [contest_id, pid],
        )
        if before is not None:
            removed += 1
    msg = f"removed {removed} problem(s)"
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message=msg)

def contest_problems_reorder(
    contest: str,
    user: str,
    contest_problem_ids: list[str] = Form([]),
    contest_problem_indices: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    ids_raw = list(contest_problem_ids or [])
    idx_raw = list(contest_problem_indices or [])
    if not ids_raw or len(ids_raw) != len(idx_raw):
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid reorder payload")
    pairs: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    seen_idx: set[str] = set()
    for i, raw_id in enumerate(ids_raw):
        try:
            cp_id = int(str(raw_id or "").strip())
        except Exception:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid contest problem id")
        if cp_id <= 0 or cp_id in seen_ids:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="invalid contest problem id")
        idx = _normalize_contest_problem_idx_required(idx_raw[i])
        if idx in seen_idx:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="duplicate problem index")
        seen_ids.add(cp_id)
        seen_idx.add(idx)
        pairs.append((cp_id, idx))
    contest_id = int(ctx["contest"]["id"])

    def _tx(conn) -> bool:
        for cp_id, _idx in pairs:
            row = conn.execute(
                "SELECT id FROM contest_problems WHERE id=? AND contest_id=?",
                [cp_id, contest_id],
            ).fetchone()
            if row is None:
                return False
        for cp_id, idx in pairs:
            temp_idx = f"TMP-{cp_id}-{secrets.token_hex(3)}"
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [temp_idx, cp_id, contest_id],
            )
        for cp_id, idx in pairs:
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [idx, cp_id, contest_id],
            )
        return True

    updated = bool(config.db.write_transaction(_tx))
    if not updated:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="contest problem not found")
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="problem order saved")

def contest_problems_renumber(contest: str, user: str):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    rows = config.db.fetch_all(
        "SELECT id FROM contest_problems WHERE contest_id=? ORDER BY idx COLLATE NOCASE ASC, id ASC",
        [contest_id],
    )

    def _tx(conn) -> None:
        seq = 1
        for row in rows:
            conn.execute(
                "UPDATE contest_problems SET idx=? WHERE id=? AND contest_id=?",
                [_contest_idx_label(seq), int(row["id"]), contest_id],
            )
            seq += 1

    config.db.write_transaction(_tx)
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="problem indices renumbered")

def contest_problems_change_general(
    contest: str,
    user: str,
    selected_problem_ids: list[str] = Form([]),
    problem_ids: list[str] = Form([]),
    problem_names: list[str] = Form([]),
    time_limit_ms_values: list[str] = Form([]),
    memory_limit_mb_values: list[str] = Form([]),
    retry_job_id: str = Form(""),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    selected_ids: list[int] = []
    for raw in list(selected_problem_ids or []):
        try:
            value = int(str(raw or "").strip())
        except Exception:
            continue
        if value > 0 and value not in selected_ids:
            selected_ids.append(value)
    requested_map = _problem_general_payload_map(
        list(problem_ids or []),
        list(problem_names or []),
        list(time_limit_ms_values or []),
        list(memory_limit_mb_values or []),
    )
    safe_retry_job_id = str(retry_job_id or "").strip()
    if safe_retry_job_id and not selected_ids:
        retry_job = _load_contest_job(contest_id, safe_retry_job_id)
        if retry_job is None:
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="retry job not found")
        if str(retry_job.get("job_type") or "") != "change-general":
            return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="retry job type is invalid")
        summary = retry_job.get("summary")
        if isinstance(summary, dict):
            results = summary.get("results")
            if isinstance(results, list):
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("status") or "").strip() != "failed":
                        continue
                    try:
                        pid = int(item.get("problem_id") or 0)
                    except Exception:
                        continue
                    if pid <= 0:
                        continue
                    req = item.get("requested")
                    if isinstance(req, dict):
                        requested_map[pid] = {
                            "name": str(req.get("name") or "").strip(),
                            "time_limit_ms": str(req.get("time_limit_ms") or "").strip(),
                            "memory_limit_mb": str(req.get("memory_limit_mb") or "").strip(),
                        }
                    if pid not in selected_ids:
                        selected_ids.append(pid)
    if not selected_ids:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="select at least one problem to update")
    placeholders = ",".join(("?" for _ in selected_ids))
    rows = config.db.fetch_all(
        f"""
        SELECT cp.idx,cp.problem_id,p.slug,p.name
        FROM contest_problems cp
        JOIN problems p ON p.id=cp.problem_id
        WHERE cp.contest_id=?
          AND cp.problem_id IN ({placeholders})
        ORDER BY cp.idx COLLATE NOCASE ASC, cp.id ASC
        """,
        [contest_id, *selected_ids],
    )
    if not rows:
        return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", message="selected problems are not part of this contest")
    results: list[dict[str, object]] = []
    for row in rows:
        pid = int(row["problem_id"])
        defaults = {
            "name": str(row["name"] or "").strip(),
            "time_limit_ms": str(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            "memory_limit_mb": str(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
        }
        requested = requested_map.get(pid, defaults)
        result = _run_problem_general_update(
            contest_slug=str(ctx["contest"]["slug"]),
            actor_username=str(ctx["user"]["username"]),
            actor_user_id=actor_user_id,
            problem_id=pid,
            problem_slug=str(row["slug"]),
            requested_name=str(requested.get("name") or ""),
            requested_time_limit_ms=str(requested.get("time_limit_ms") or ""),
            requested_memory_limit_mb=str(requested.get("memory_limit_mb") or ""),
        )
        results.append(result)
    success_count = sum((1 for row in results if str(row.get("status") or "") == "success"))
    failed_count = sum((1 for row in results if str(row.get("status") or "") == "failed"))
    skipped_count = sum((1 for row in results if str(row.get("status") or "") == "skipped"))
    summary = {
        "contest_slug": str(ctx["contest"]["slug"]),
        "job_type": "change-general",
        "results": results,
        "totals": {
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
        },
    }
    job_status = "ok" if failed_count == 0 else "failed"
    job_id = _create_contest_job(contest_id, actor_user_id, "change-general", job_status, summary)
    audit(
        actor_user_id,
        None,
        "contest.problems.change_general",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "job_id": job_id,
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "skipped": skipped_count,
        },
    )
    msg = f"change names/TL/ML finished: {success_count} success, {failed_count} failed, {skipped_count} skipped"
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "problems", query=f"job_id={quote_plus(job_id)}", message=msg)


