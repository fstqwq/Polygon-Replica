import app.main_constant as _K
from app.impl.auth.session import require_session_user
from typing import Annotated

from urllib.parse import quote_plus

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import template_response
from app.impl.contest.workspace_scope import add_contest_problem_hrefs
from app.impl.runtime.dependency import runtime
from app.main_util import form_text

from app.impl.contest.common import (
    _dedupe_preserve,
    _normalize_contest_problem_idx_required,
)
from app.impl.contest.shared import (
    _contest_ctx,
    _contest_redirect,
    _problem_general_payload_map,
    _run_problem_general_update,
)



def contest_problems_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)], q: str = "", job_id: str = ""):
    ctx = _contest_ctx(contest, user, "problems")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = add_contest_problem_hrefs(
        request,
        contest_slug=str(ctx["contest"]["slug"]),
        rows=runtime().contest_problem_query_service.problem_rows(
            contest_id,
            str(ctx["user"]["username"]),
            user_id,
            include_review=False,
        ),
    )
    query = q.strip()
    available_rows = runtime().contest_service.available_problems(
        contest_id,
        user_id,
        limit=runtime().config_values.integer("API_PROBLEMS_LIST_LIMIT"),
        query=query,
    )
    available_display_rows: list[dict[str, object]] = []
    for row in available_rows:
        slug_owner, _separator, slug_leaf = row["problem_slug"].partition("/")
        available_display_rows.append(
            {
                **row,
                "slug_owner": slug_owner,
                "slug_leaf": slug_leaf,
                "href": str(request.url_for("problem_statement", problem=row["problem_slug"])),
            }
        )
    owner_prefix_chars = max(
        (len(str(row["slug_owner"])) + 1 for row in rows),
        default=0,
    )
    available_owner_prefix_chars = max(
        (len(str(row["slug_owner"])) + 1 for row in available_display_rows),
        default=0,
    )
    latest_job = runtime().contest_service.load_job(contest_id, job_id)
    return template_response(
        request,
        "contest_problems.html",
        {
            "ctx": ctx,
            "query": query,
            "problem_rows": rows,
            "available_rows": available_display_rows,
            "owner_prefix_chars": owner_prefix_chars,
            "available_owner_prefix_chars": available_owner_prefix_chars,
            "latest_job": latest_job,
        },
    )


def contest_problems_add(contest: str, user: Annotated[str, Depends(require_session_user)], problem_slugs: list[str] = Form([]), q: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_manage_roster"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["roster_block_reason"])
    safe_slugs = _dedupe_preserve([form_text(item) for item in problem_slugs])
    if not safe_slugs:
        safe_query = q.strip()
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "problems",
            query=f"q={quote_plus(safe_query)}" if safe_query else "",
            message="select at least one problem to add",
        )
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    added = 0
    failed: list[str] = []
    for slug in safe_slugs:
        problem_row = runtime().contest_service.problem_by_slug(slug)
        if problem_row is None:
            failed.append(f"{slug}: problem not found")
            continue
        problem_id = int(problem_row["id"])
        problem_access = runtime().access_query.direct_problem_context(problem_id, user_id)
        if not problem_access["can_manage"]:
            failed.append(f"{slug}: direct problem manage access required")
            continue
        if runtime().contest_service.contest_has_problem(contest_id, problem_id):
            failed.append(f"{slug}: already in contest")
            continue
        try:
            idx = runtime().contest_service.next_problem_index(contest_id)
            runtime().contest_service.add_problem(contest_id, idx, problem_id, user_id)
            added += 1
        except Exception as exc:
            failed.append(f"{slug}: {exc}")
    msg = f"added {added} problem(s)"
    if failed:
        msg += f"; failed {len(failed)}"
    query_parts: list[str] = []
    safe_query = q.strip()
    if safe_query:
        query_parts.append(f"q={quote_plus(safe_query)}")
    return _contest_redirect(str(ctx["contest"]["slug"]), "problems", query="&".join(query_parts), message=msg)


def contest_problems_remove(contest: str, user: Annotated[str, Depends(require_session_user)], problem_id: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_manage_roster"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["roster_block_reason"])
    msg = "problem removed"
    try:
        pid = int(problem_id)
    except Exception:
        pid = 0
    if pid <= 0:
        msg = "invalid problem id"
    else:
        runtime().contest_service.remove_problem(int(ctx["contest"]["id"]), pid)
    return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message=msg)


def contest_problems_remove_selected(contest: str, user: Annotated[str, Depends(require_session_user)], selected_problem_ids: list[str] = Form([])):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_manage_roster"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["roster_block_reason"])
    ids: list[int] = []
    for raw in selected_problem_ids:
        try:
            value = int(raw)
        except Exception:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message="select at least one problem to remove")
    removed = runtime().contest_service.remove_problems(int(ctx["contest"]["id"]), ids)
    return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message=f"removed {removed} problem(s)")


def _contest_problem_index_pairs(
    contest_problem_ids: list[str],
    contest_problem_indices: list[str],
) -> list[tuple[int, str]]:
    if not contest_problem_ids or len(contest_problem_ids) != len(contest_problem_indices):
        raise ValueError("invalid problem order payload")
    pairs: list[tuple[int, str]] = []
    seen_ids: set[int] = set()
    seen_indices: set[str] = set()
    for raw_id, raw_index in zip(contest_problem_ids, contest_problem_indices, strict=True):
        try:
            contest_problem_id = int(raw_id)
        except ValueError as exc:
            raise ValueError("invalid contest problem id") from exc
        if contest_problem_id <= 0 or contest_problem_id in seen_ids:
            raise ValueError("invalid contest problem id")
        index = _normalize_contest_problem_idx_required(raw_index)
        if index in seen_indices:
            raise ValueError("duplicate problem index")
        seen_ids.add(contest_problem_id)
        seen_indices.add(index)
        pairs.append((contest_problem_id, index))
    return pairs


def contest_problems_reorder(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    contest_problem_ids: list[str] = Form([]),
    contest_problem_indices: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_manage_roster"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["roster_block_reason"])
    try:
        pairs = _contest_problem_index_pairs(contest_problem_ids, contest_problem_indices)
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message=str(exc))
    if not runtime().contest_service.reorder_problem_indices(int(ctx["contest"]["id"]), pairs):
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "problems",
            message="problem order must include every contest problem",
        )
    return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message="problem order saved")


def contest_problems_renumber(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    contest_problem_ids: list[str] = Form([]),
    contest_problem_indices: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_manage_roster"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["roster_block_reason"])
    try:
        pairs = _contest_problem_index_pairs(contest_problem_ids, contest_problem_indices)
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message=str(exc))
    ordered_ids = [
        contest_problem_id
        for contest_problem_id, _ in sorted(pairs, key=lambda pair: pair[1])
    ]
    if not runtime().contest_service.renumber_problem_indices(int(ctx["contest"]["id"]), ordered_ids):
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "problems",
            message="problem order must include every contest problem",
        )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "problems",
        message="problem indices renumbered",
    )


def _selected_problem_ids(raw_ids: list[str]) -> list[int]:
    selected_ids: list[int] = []
    for raw_id in raw_ids:
        try:
            problem_id = int(raw_id)
        except ValueError:
            continue
        if problem_id > 0 and problem_id not in selected_ids:
            selected_ids.append(problem_id)
    return selected_ids


def _failed_general_job_payload(
    contest_id: int,
    retry_job_id: str,
) -> tuple[list[int], dict[int, dict[str, object]]]:
    retry_job = runtime().contest_service.load_job(contest_id, retry_job_id)
    if retry_job is None:
        raise ValueError("retry job not found")
    if retry_job["job_type"] != "change-general":
        raise ValueError("retry job type is invalid")
    summary = retry_job["summary"]
    if not isinstance(summary, dict):
        raise ValueError("retry job report is invalid")
    retry_results = summary.get("results")
    if not isinstance(retry_results, list):
        raise ValueError("retry job report is invalid")
    selected_ids: list[int] = []
    requested_map: dict[int, dict[str, object]] = {}
    for result in retry_results:
        if not isinstance(result, dict) or result.get("status") != "failed":
            continue
        problem_id = result.get("problem_id")
        requested = result.get("requested")
        if not isinstance(problem_id, int) or problem_id <= 0 or not isinstance(requested, dict):
            raise ValueError("retry job report is invalid")
        time_limit_ms = requested.get("time_limit_ms")
        memory_limit_mb = requested.get("memory_limit_mb")
        if not isinstance(time_limit_ms, str) or not isinstance(memory_limit_mb, str):
            raise ValueError("retry job report is invalid")
        selected_ids.append(problem_id)
        requested_map[problem_id] = {
            "time_limit_ms": time_limit_ms,
            "memory_limit_mb": memory_limit_mb,
        }
    if not selected_ids:
        raise ValueError("retry job has no failed problems")
    return selected_ids, requested_map


def _apply_general_changes(
    ctx: dict[str, object],
    selected_ids: list[int],
    requested_map: dict[int, dict[str, object]],
):
    contest_ctx = ctx["contest"]
    user_ctx = ctx["user"]
    if not isinstance(contest_ctx, dict) or not isinstance(user_ctx, dict):
        raise RuntimeError("invalid contest context")
    contest_id = int(contest_ctx["id"])
    actor_user_id = int(user_ctx["id"])
    if not selected_ids:
        return _contest_redirect(
            str(contest_ctx["slug"]),
            "problems",
            message="select at least one problem to update",
        )
    rows = runtime().contest_service.selected_problems(contest_id, selected_ids)
    if len(rows) != len(selected_ids):
        return _contest_redirect(
            str(contest_ctx["slug"]),
            "problems",
            message="selected problems are not part of this contest",
        )
    results: list[dict[str, object]] = []
    for row in rows:
        pid = int(row["problem_id"])
        defaults = {
            "time_limit_ms": str(_K.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
            "memory_limit_mb": str(_K.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
        }
        requested = requested_map.get(pid, defaults)
        requested_time_limit = requested.get("time_limit_ms")
        requested_memory_limit = requested.get("memory_limit_mb")
        result = _run_problem_general_update(
            contest_slug=str(contest_ctx["slug"]),
            actor_username=str(user_ctx["username"]),
            actor_user_id=actor_user_id,
            problem_id=pid,
            problem_slug=str(row["problem_slug"]),
            requested_time_limit_ms=(
                requested_time_limit if isinstance(requested_time_limit, str) else ""
            ),
            requested_memory_limit_mb=(
                requested_memory_limit
                if isinstance(requested_memory_limit, str)
                else ""
            ),
        )
        results.append(result)
    success_count = sum((1 for row in results if isinstance(row.get("status"), str) and row["status"] == "success"))
    failed_count = sum((1 for row in results if isinstance(row.get("status"), str) and row["status"] == "failed"))
    skipped_count = sum((1 for row in results if isinstance(row.get("status"), str) and row["status"] == "skipped"))
    summary: dict[str, object] = {
        "contest_slug": str(contest_ctx["slug"]),
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
    job_id = runtime().contest_service.create_job(contest_id, actor_user_id, "change-general", job_status, summary)
    return _contest_redirect(
        str(contest_ctx["slug"]),
        "problems",
        query=f"job_id={quote_plus(job_id)}",
        message=f"change TL/ML finished: {success_count} success, {failed_count} failed, {skipped_count} skipped",
    )


def contest_problems_change_general(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    selected_problem_ids: list[str] = Form([]),
    problem_ids: list[str] = Form([]),
    time_limit_ms_values: list[str] = Form([]),
    memory_limit_mb_values: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    selected_ids = _selected_problem_ids(selected_problem_ids)
    requested_map = _problem_general_payload_map(
        problem_ids,
        time_limit_ms_values,
        memory_limit_mb_values,
    )
    return _apply_general_changes(ctx, selected_ids, requested_map)


def contest_problems_change_general_retry(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    retry_job_id: str = Form(...),
):
    ctx = _contest_ctx(contest, user, "problems")
    if not bool(ctx["access"].get("can_write")):
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
    try:
        selected_ids, requested_map = _failed_general_job_payload(
            int(ctx["contest"]["id"]),
            retry_job_id,
        )
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "problems", message=str(exc))
    return _apply_general_changes(ctx, selected_ids, requested_map)
