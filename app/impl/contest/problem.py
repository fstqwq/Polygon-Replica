from typing import Annotated
from urllib.parse import quote_plus

from fastapi import Depends, Form, HTTPException, Request

import app.main_constant as _K
from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.common import _dedupe_preserve
from app.impl.contest.shared import (
    _contest_ctx,
    _contest_redirect,
    _problem_general_payload_map,
    _run_problem_general_update,
)
from app.impl.contest.workspace_scope import add_contest_problem_hrefs
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_model import workspace_published_revision_pair
from app.main_util import form_text
from app.service.contest.problem_index import normalize_contest_problem_idx


def contest_problems_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)], q: str = ""):
    ctx = _contest_ctx(contest, user, "problems", request=request)
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    source_rows = add_contest_problem_hrefs(
        request,
        contest_slug=str(ctx["contest"]["slug"]),
        rows=runtime().contest_problem_query_service.problem_rows(
            contest_id,
            str(ctx["user"]["username"]),
            user_id,
            include_review=False,
        ),
    )
    rows: list[dict[str, object]] = []
    for source_row in source_rows:
        row = dict(source_row)
        row["workspace_revision_pair"] = (
            workspace_published_revision_pair(
                source_row["workspace_revision_local"],
                source_row["workspace_revision_upstream"],
                dirty=source_row["dirty"],
                needs_update=source_row["workspace_revision_warn"],
            )
            if source_row["workspace_revision_available"]
            else None
        )
        rows.append(row)
    query = q.strip()
    available_rows = runtime().contest_service.available_problems(
        contest_id,
        user_id,
        limit=runtime().config_values.integer("API_PROBLEMS_LIST_LIMIT"),
        query=query,
    )
    available_display_rows: list[dict[str, object]] = []
    for available_row in available_rows:
        slug_owner, _separator, slug_leaf = available_row["problem_slug"].partition("/")
        available_display_rows.append(
            {
                **available_row,
                "slug_owner": slug_owner,
                "slug_leaf": slug_leaf,
                "href": str(
                    request.url_for(
                        "problem_statement",
                        problem=available_row["problem_slug"],
                    )
                ),
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
        },
    )


def contest_problems_add(contest: str, user: Annotated[str, Depends(require_session_user)], problem_slugs: list[str] = Form([]), q: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    if not ctx["access"]["can_write"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["write_block_reason"])
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
        if not problem_access["can_write"]:
            failed.append(f"{slug}: direct problem write access required")
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
    if added:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "overview",
            message=msg,
        )
    safe_query = q.strip()
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "problems",
        query=f"q={quote_plus(safe_query)}" if safe_query else "",
        message=msg,
    )


def contest_problems_remove(contest: str, user: Annotated[str, Depends(require_session_user)], problem_id: str = Form("")):
    ctx = _contest_ctx(contest, user, "problems")
    contest_access = ctx["access"]
    if not contest_access["can_write"]:
        raise HTTPException(status_code=403, detail=contest_access["write_block_reason"])
    msg = "problem removed"
    try:
        pid = int(problem_id)
    except Exception:
        pid = 0
    if pid <= 0:
        msg = "invalid problem id"
    else:
        contest_id = int(ctx["contest"]["id"])
        selected = runtime().contest_service.selected_problems(contest_id, [pid])
        if not selected:
            msg = "problem is not part of this contest"
        else:
            if not contest_access["can_manage_roster"]:
                direct_access = runtime().access_query.direct_problem_context(
                    pid,
                    int(ctx["user"]["id"]),
                )
                if not direct_access["can_write"]:
                    raise HTTPException(
                        status_code=403,
                        detail="direct problem write access required",
                    )
            runtime().contest_service.remove_problem(contest_id, pid)
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "overview" if pid > 0 else "problems",
        message=msg,
    )


def contest_problems_remove_selected(contest: str, user: Annotated[str, Depends(require_session_user)], selected_problem_ids: list[str] = Form([])):
    ctx = _contest_ctx(contest, user, "problems")
    contest_access = ctx["access"]
    if not contest_access["can_write"]:
        raise HTTPException(status_code=403, detail=contest_access["write_block_reason"])
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
    contest_id = int(ctx["contest"]["id"])
    selected = runtime().contest_service.selected_problems(contest_id, ids)
    selected_ids = [int(row["problem_id"]) for row in selected]
    removable_ids = selected_ids
    if not contest_access["can_manage_roster"]:
        direct_access = runtime().access_query.direct_problem_contexts(
            selected_ids,
            int(ctx["user"]["id"]),
        )
        removable_ids = [
            selected_problem_id
            for selected_problem_id in selected_ids
            if direct_access[selected_problem_id]["can_write"]
        ]
    removed = runtime().contest_service.remove_problems(contest_id, removable_ids)
    failed = len(ids) - removed
    message = f"removed {removed} problem(s)"
    if failed:
        message += f"; failed {failed}"
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "overview",
        message=message,
    )


def _contest_problem_index_pairs(
    contest_problem_ids: list[str],
    contest_problem_indices: list[str],
) -> list[tuple[int, str]]:
    if not contest_problem_ids or len(contest_problem_ids) != len(contest_problem_indices):
        raise ValueError("invalid problem index payload")
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
        index = normalize_contest_problem_idx(raw_index)
        if index in seen_indices:
            raise ValueError("duplicate problem index")
        seen_ids.add(contest_problem_id)
        seen_indices.add(index)
        pairs.append((contest_problem_id, index))
    return pairs


def _apply_general_changes(
    ctx: dict[str, object],
    selected_ids: list[int],
    requested_map: dict[int, dict[str, object]],
    *,
    message_prefix: str = "",
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
    success_count = 0
    failed_count = 0
    skipped_count = 0
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
        status = result.get("status")
        if status == "success":
            success_count += 1
        elif status == "failed":
            failed_count += 1
        else:
            skipped_count += 1
    return _contest_redirect(
        str(contest_ctx["slug"]),
        "overview",
        message=(
            f"{message_prefix}; " if message_prefix else ""
        ) + (
            f"limits: {success_count} saved, {failed_count} failed, "
            f"{skipped_count} unchanged"
        ),
    )


def contest_problems_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    contest_problem_ids: list[str] = Form([]),
    contest_problem_indices: list[str] = Form([]),
    problem_ids: list[str] = Form([]),
    time_limit_ms_values: list[str] = Form([]),
    memory_limit_mb_values: list[str] = Form([]),
    original_time_limit_ms_values: list[str] = Form([]),
    original_memory_limit_mb_values: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "problems")
    contest_access = ctx["access"]
    can_manage_roster = bool(contest_access.get("can_manage_roster"))
    can_write = bool(contest_access.get("can_write"))
    if not can_manage_roster and not can_write:
        raise HTTPException(
            status_code=403,
            detail=contest_access["write_block_reason"],
        )

    if can_manage_roster:
        try:
            pairs = _contest_problem_index_pairs(
                contest_problem_ids,
                contest_problem_indices,
            )
        except ValueError as exc:
            return _contest_redirect(
                str(ctx["contest"]["slug"]),
                "problems",
                message=str(exc),
            )
        try:
            runtime().contest_service.set_problem_indices(
                int(ctx["contest"]["id"]),
                pairs,
            )
        except ValueError as exc:
            return _contest_redirect(
                str(ctx["contest"]["slug"]),
                "problems",
                message=str(exc),
            )

    if not can_write:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "overview",
            message="contest problems saved",
        )

    requested_map = _problem_general_payload_map(
        problem_ids,
        time_limit_ms_values,
        memory_limit_mb_values,
    )
    original_map = _problem_general_payload_map(
        problem_ids,
        original_time_limit_ms_values,
        original_memory_limit_mb_values,
    )
    requested_ids = [
        problem_id
        for problem_id, requested in requested_map.items()
        if requested != original_map.get(problem_id)
    ]
    if not requested_ids:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "overview",
            message="contest problems saved",
        )
    problem_access = runtime().access_query.problem_contexts(
        requested_ids,
        int(ctx["user"]["id"]),
    )
    writable_ids = [
        problem_id
        for problem_id in requested_ids
        if problem_access[problem_id]["can_write"]
    ]
    if not writable_ids:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "overview",
            message="contest problems saved",
        )
    return _apply_general_changes(
        ctx,
        writable_ids,
        requested_map,
        message_prefix="contest problems saved",
    )
