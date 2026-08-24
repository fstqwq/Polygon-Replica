"""Contest membership and direct Problem ACL management."""

from typing import Annotated, cast
from urllib.parse import urlencode

from fastapi import Depends, Form, HTTPException, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.shared import _contest_ctx, _contest_redirect
from app.impl.runtime.dependency import runtime
from app.service.access.errors import AccessConflictError
from app.service.access.model import DirectProblemRole, ProblemAccessChange
from app.service.access.policy import transferable_contest_role


def _positive_query_ids(request: Request, key: str) -> list[int]:
    result: list[int] = []
    for raw in request.query_params.getlist(key):
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0 and value not in result:
            result.append(value)
    return result


def _contest_problem_access_matrix(
    *,
    contest_id: int,
    actor_user_id: int,
    can_manage_contest: bool,
    focus_problem_ids: list[int],
    focus_user_ids: list[int],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[int], int]:
    problems = runtime().contest_service.contest_problems(contest_id)
    members = runtime().contest_service.member_entries(contest_id)
    problem_ids = [int(row["problem_id"]) for row in problems]
    user_ids = [int(row["user_id"]) for row in members]
    actor_access = runtime().access_query.direct_problem_contexts(
        problem_ids,
        actor_user_id,
    )
    roles = runtime().access_query.direct_problem_roles_for_users(
        problem_ids,
        user_ids,
    )

    valid_problem_focus = {
        int(row["contest_problem_id"])
        for row in problems
    }.intersection(focus_problem_ids)
    valid_user_focus = set(user_ids).intersection(focus_user_ids)
    matrix_rows: list[dict[str, object]] = []
    editable_by_user: dict[int, list[bool]] = {user_id: [] for user_id in user_ids}
    for problem in problems:
        problem_id = int(problem["problem_id"])
        row_can_manage = bool(
            can_manage_contest
            and actor_access[problem_id]["can_manage_access"]
        )
        cells: list[dict[str, object]] = []
        for member in members:
            target_user_id = int(member["user_id"])
            role = roles.get((problem_id, target_user_id), "none")
            is_owner = role == "owner"
            is_admin = int(member["is_system_admin"] or 0) == 1
            is_self = target_user_id == actor_user_id
            fixed = is_owner or is_admin or is_self
            can_edit = row_can_manage and not fixed
            if is_owner:
                display_role = "owner"
                fixed_reason = "owner"
            elif is_admin:
                display_role = "admin"
                fixed_reason = "system admin"
            elif is_self:
                display_role = str(role)
                fixed_reason = "your access"
            else:
                display_role = str(role)
                fixed_reason = ""
            cells.append(
                {
                    "target_user_id": target_user_id,
                    "role": str(role),
                    "display_role": display_role,
                    "can_edit": can_edit,
                    "fixed": fixed,
                    "fixed_reason": fixed_reason,
                    "focused": target_user_id in valid_user_focus,
                }
            )
            if not fixed:
                editable_by_user[target_user_id].append(can_edit)
        matrix_rows.append(
            {
                **problem,
                "cells": cells,
                "can_bulk_edit": any(bool(cell["can_edit"]) for cell in cells),
                "focused": int(problem["contest_problem_id"])
                in valid_problem_focus,
            }
        )

    matrix_members: list[dict[str, object]] = []
    for member in members:
        target_user_id = int(member["user_id"])
        editable = editable_by_user[target_user_id]
        matrix_members.append(
            {
                **member,
                "can_bulk_edit": any(editable),
                "can_exit": (
                    target_user_id == actor_user_id
                    and str(member["role"]) != "owner"
                ),
                "focused": target_user_id in valid_user_focus,
            }
        )
    ordered_problem_focus = [
        value for value in focus_problem_ids if value in valid_problem_focus
    ]
    first_focus_user = next(
        (value for value in focus_user_ids if value in valid_user_focus),
        0,
    )
    return matrix_rows, matrix_members, ordered_problem_focus, first_focus_user


def contest_access_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "access", request=request)
    contest_id = int(ctx["contest"]["id"])
    focus_problem_ids = _positive_query_ids(request, "focus_problem_id")
    focus_user_ids = _positive_query_ids(request, "focus_user_id")
    matrix_rows, entries, valid_problem_focus, valid_user_focus = (
        _contest_problem_access_matrix(
            contest_id=contest_id,
            actor_user_id=int(ctx["user"]["id"]),
            can_manage_contest=bool(ctx["access"]["can_manage"]),
            focus_problem_ids=focus_problem_ids,
            focus_user_ids=focus_user_ids,
        )
    )
    ctx["page_single_column"] = True
    return template_response(
        request,
        "contest_access.html",
        {
            "ctx": ctx,
            "entries": entries,
            "show_member_actions": bool(
                ctx["access"]["can_manage"]
                or any(bool(entry["can_exit"]) for entry in entries)
            ),
            "owner_count": runtime().contest_service.owner_count(contest_id),
            "matrix_rows": matrix_rows,
            "matrix_has_editable": any(
                bool(row["can_bulk_edit"]) for row in matrix_rows
            ),
            "repo_role_options": ["none", "read", "write"],
            "focus_problem_ids": valid_problem_focus,
            "focus_user_id": valid_user_focus,
        },
    )


def contest_access_grant(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    target_user: str = Form(...),
    role: str = Form("read"),
):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["manage_block_reason"],
        )
    safe_target = target_user.strip()
    try:
        safe_role = transferable_contest_role(role)
        result = runtime().access_command.set_contest_membership(
            actor_user_id=int(ctx["user"]["id"]),
            contest_id=int(ctx["contest"]["id"]),
            target_username=safe_target,
            role=safe_role,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "access",
            message=str(exc),
        )
    query = urlencode({"focus_user_id": int(result["target_user_id"])})
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "access",
        query=query,
        fragment="problem-access-matrix",
        message=f"granted {safe_role} to {result['target_username']}",
    )


def contest_access_revoke(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    target_user: str = Form(...),
):
    ctx = _contest_ctx(contest, user, "access")
    safe_target = target_user.strip()
    try:
        result = runtime().access_command.revoke_contest_membership(
            actor_user_id=int(ctx["user"]["id"]),
            contest_id=int(ctx["contest"]["id"]),
            target_username=safe_target,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "access",
            message=str(exc),
        )
    if int(result["target_user_id"]) == int(ctx["user"]["id"]):
        return redirect_response(
            "/contests",
            status_code=303,
            message=f"left contest {ctx['contest']['slug']}",
        )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "access",
        message=f"revoked contest membership for {result['target_username']}",
    )


def _matrix_cell_key(raw_key: str, prefix: str) -> tuple[int, int]:
    parts = raw_key.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        raise ValueError("invalid problem access field")
    try:
        problem_id = int(parts[1])
        target_user_id = int(parts[2])
    except ValueError as exc:
        raise ValueError("invalid problem access field") from exc
    if problem_id <= 0 or target_user_id <= 0:
        raise ValueError("invalid problem access field")
    return problem_id, target_user_id


def _direct_problem_role(raw_role: str) -> DirectProblemRole:
    if raw_role in {"none", "read", "write", "owner"}:
        return cast(DirectProblemRole, raw_role)
    raise ValueError("invalid problem access role")


async def _problem_access_changes(request: Request) -> list[ProblemAccessChange]:
    form = await request.form()
    requested: dict[tuple[int, int], DirectProblemRole] = {}
    original: dict[tuple[int, int], DirectProblemRole] = {}
    for key, raw_value in form.multi_items():
        if key.startswith("role."):
            pair = _matrix_cell_key(key, "role")
            target = requested
        elif key.startswith("original_role."):
            pair = _matrix_cell_key(key, "original_role")
            target = original
        else:
            continue
        if pair in target:
            raise ValueError("duplicate problem access cell")
        target[pair] = _direct_problem_role(str(raw_value))
    if requested.keys() != original.keys():
        raise ValueError("incomplete problem access payload")
    if not requested:
        raise ValueError("no editable problem access cells were submitted")
    return [
        ProblemAccessChange(
            problem_id=problem_id,
            target_user_id=target_user_id,
            original_role=original[(problem_id, target_user_id)],
            requested_role=requested[(problem_id, target_user_id)],
        )
        for problem_id, target_user_id in sorted(requested)
    ]


async def contest_problem_access_save(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["manage_block_reason"],
        )
    try:
        changes = await _problem_access_changes(request)
        changed = runtime().access_command.save_contest_problem_access(
            actor_user_id=int(ctx["user"]["id"]),
            contest_id=int(ctx["contest"]["id"]),
            changes=changes,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AccessConflictError as exc:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "access",
            fragment="problem-access-matrix",
            message=f"nothing was saved: {exc}",
        )
    except ValueError as exc:
        return _contest_redirect(
            str(ctx["contest"]["slug"]),
            "access",
            fragment="problem-access-matrix",
            message=str(exc),
        )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "access",
        fragment="problem-access-matrix",
        message=f"updated {changed} problem access cell(s)",
    )
