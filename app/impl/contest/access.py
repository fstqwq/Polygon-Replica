from __future__ import annotations
from app.impl.auth.session import require_session_user
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import normalize_username_required, template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit
from app.impl.workspace.access import workspace_access_context

from .common import _normalize_transferable_contest_member_role_required
from .shared import _contest_ctx, _contest_redirect


def _contest_sync_candidate_problems(contest_id: int, actor_user_id: int) -> tuple[list[dict[str, object]], int]:
    eligible: list[dict[str, object]] = []
    skipped = 0
    for row in config.contest_service.contest_problems(int(contest_id)):
        problem_id = int(row["problem_id"])
        problem_slug = str(row["problem_slug"])
        actor_access = workspace_access_context(problem_id, int(actor_user_id))
        if bool(actor_access.get("can_write")):
            eligible.append({"problem_id": problem_id, "problem_slug": problem_slug})
        else:
            skipped += 1
    return eligible, skipped


def _contest_member_problem_sync_plan(
    contest_id: int,
    actor_user_id: int,
    target_username: str,
    *,
    desired_role: str | None = None,
) -> dict[str, object]:
    safe_target = normalize_username_required(target_username)
    target_user = config.workspace_service.known_user(safe_target)
    if int(target_user.get("is_system_admin") or 0) == 1:
        return {
            "target_username": safe_target,
            "target_user_id": int(target_user["id"]),
            "desired_role": "admin",
            "eligible_problem_count": 0,
            "skipped_problem_count": 0,
            "change_problem_ids": [],
            "change_problem_slugs": [],
            "unchanged_problem_count": 0,
            "owner_fixed_problem_count": 0,
            "system_admin_passthrough": True,
        }
    membership = config.contest_service.membership_for_username(int(contest_id), safe_target)
    if membership is None:
        raise ValueError(f"{safe_target} is not a member")
    safe_role = str(desired_role or membership["role"]).strip().lower()
    if safe_role not in {"read", "write"}:
        return {
            "target_username": safe_target,
            "target_user_id": int(target_user["id"]),
            "desired_role": safe_role or "none",
            "eligible_problem_count": 0,
            "skipped_problem_count": 0,
            "change_problem_ids": [],
            "change_problem_slugs": [],
            "unchanged_problem_count": 0,
            "owner_fixed_problem_count": 0,
            "system_admin_passthrough": False,
        }
    eligible, skipped = _contest_sync_candidate_problems(int(contest_id), int(actor_user_id))
    change_problem_ids: list[int] = []
    change_problem_slugs: list[str] = []
    unchanged_problem_count = 0
    owner_fixed_problem_count = 0
    for item in eligible:
        problem_id = int(item["problem_id"])
        problem_slug = str(item["problem_slug"])
        access = workspace_access_context(problem_id, int(target_user["id"]))
        current_role = str(access.get("role") or "none").strip().lower()
        if current_role == "owner":
            owner_fixed_problem_count += 1
            continue
        if current_role == "admin":
            unchanged_problem_count += 1
            continue
        if current_role == safe_role:
            unchanged_problem_count += 1
            continue
        change_problem_ids.append(problem_id)
        change_problem_slugs.append(problem_slug)
    return {
        "target_username": safe_target,
        "target_user_id": int(target_user["id"]),
        "desired_role": safe_role,
        "eligible_problem_count": len(eligible),
        "skipped_problem_count": skipped,
        "change_problem_ids": change_problem_ids,
        "change_problem_slugs": change_problem_slugs,
        "unchanged_problem_count": unchanged_problem_count,
        "owner_fixed_problem_count": owner_fixed_problem_count,
        "system_admin_passthrough": False,
    }


def _sync_contest_member_problem_access(contest_id: int, actor_user_id: int, target_username: str) -> dict[str, object]:
    plan = _contest_member_problem_sync_plan(int(contest_id), int(actor_user_id), target_username)
    if bool(plan.get("system_admin_passthrough")):
        return {**plan, "changed_count": 0}
    desired_role = str(plan["desired_role"])
    safe_target = str(plan["target_username"])
    changed_count = 0
    for problem_id in plan["change_problem_ids"]:
        config.workspace_service.set_repo_access_for_problem_id(int(problem_id), safe_target, desired_role)
        changed_count += 1
    return {**plan, "changed_count": changed_count}


def _grant_sync_reminder_message(contest_id: int, actor_user_id: int, target_username: str, role: str) -> str:
    plan = _contest_member_problem_sync_plan(int(contest_id), int(actor_user_id), target_username, desired_role=role)
    safe_target = str(plan["target_username"])
    if bool(plan.get("system_admin_passthrough")):
        return f"granted {role} to {safe_target}; no problem sync needed because the user is a system admin"
    eligible_problem_count = int(plan["eligible_problem_count"])
    skipped_problem_count = int(plan["skipped_problem_count"])
    change_count = len(list(plan["change_problem_ids"]))
    if eligible_problem_count <= 0:
        return f"granted {role} to {safe_target}; note: you currently have no writable contest problems to sync"
    if change_count <= 0:
        if skipped_problem_count > 0:
            return (
                f"granted {role} to {safe_target}; problem access already matches on {eligible_problem_count} writable contest "
                f"problem(s), skipped {skipped_problem_count} unwritable contest problem(s)"
            )
        return f"granted {role} to {safe_target}; problem access already matches on {eligible_problem_count} writable contest problem(s)"
    preview = ", ".join(list(plan["change_problem_slugs"])[:3])
    detail = f" sync {change_count} writable contest problem(s)"
    if preview:
        detail += f" ({preview}"
        if change_count > 3:
            detail += ", ..."
        detail += ")"
    if skipped_problem_count > 0:
        detail += f"; {skipped_problem_count} unwritable contest problem(s) were skipped"
    return f"granted {role} to {safe_target}; reminder:{detail}"


def contest_access_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "access")
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    return template_response(
        request,
        "contest_access.html",
        {
            "ctx": ctx,
            "entries": config.contest_service.member_entries(contest_id),
            "owner_count": config.contest_service.owner_count(contest_id),
            "sync_problem_candidate_count": len(_contest_sync_candidate_problems(contest_id, actor_user_id)[0]),
        },
    )


def contest_access_grant(contest: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...), role: str = Form("read")):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = target_user.strip()
    try:
        safe_role = _normalize_transferable_contest_member_role_required(role)
        if not config.contest_service.grant_member_role(contest_id, safe_target, safe_role):
            return _contest_redirect(
                str(ctx["contest"]["slug"]),
                "access",
                message=f"user {safe_target} not found; ask them to register first",
            )
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=str(exc))
    audit(
        actor_user_id,
        None,
        "contest.access.grant",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
            "role": safe_role,
        },
    )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "access",
        message=_grant_sync_reminder_message(contest_id, actor_user_id, safe_target, safe_role),
    )


def contest_access_revoke(contest: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...)):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = target_user.strip()
    membership = config.contest_service.membership_for_username(contest_id, safe_target)
    if membership is None:
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=f"{safe_target} is not a member")
    if membership["role"] == "owner":
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message="owner access is fixed and cannot be transferred")
    config.contest_service.revoke_member(contest_id, membership["user_id"])
    audit(
        actor_user_id,
        None,
        "contest.access.revoke",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=f"revoked access for {safe_target}")


def contest_access_sync_user(contest: str, user: Annotated[str, Depends(require_session_user)], target_user: str = Form(...)):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    safe_target = normalize_username_required(target_user)
    try:
        result = _sync_contest_member_problem_access(contest_id, actor_user_id, safe_target)
    except ValueError as exc:
        return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=str(exc))
    audit(
        actor_user_id,
        None,
        "contest.access.sync-user-problems",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "target_user": safe_target,
            "changed_count": int(result["changed_count"]),
            "eligible_problem_count": int(result["eligible_problem_count"]),
            "skipped_problem_count": int(result["skipped_problem_count"]),
        },
    )
    if bool(result.get("system_admin_passthrough")):
        msg = f"no sync needed for {safe_target}; system admins already have full problem access"
    else:
        msg = (
            f"synced {int(result['changed_count'])} problem access entry(s) for {safe_target}; "
            f"{int(result['eligible_problem_count'])} writable contest problem(s), "
            f"{int(result['skipped_problem_count'])} unwritable contest problem(s) skipped"
        )
    return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=msg)


def contest_access_sync_all(contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "access")
    if not bool(ctx["access"].get("can_manage")):
        raise HTTPException(status_code=403, detail=ctx["access"]["manage_block_reason"])
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    entries = config.contest_service.member_entries(contest_id)
    members_processed = 0
    changed_total = 0
    eligible_problem_count = len(_contest_sync_candidate_problems(contest_id, actor_user_id)[0])
    skipped_problem_count = 0
    system_admin_members = 0
    for row in entries:
        role = str(row["role"]).strip().lower()
        if role not in {"read", "write"}:
            continue
        result = _sync_contest_member_problem_access(contest_id, actor_user_id, str(row["username"]))
        members_processed += 1
        changed_total += int(result["changed_count"])
        skipped_problem_count = max(skipped_problem_count, int(result["skipped_problem_count"]))
        if bool(result.get("system_admin_passthrough")):
            system_admin_members += 1
    audit(
        actor_user_id,
        None,
        "contest.access.sync-all-problems",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "members_processed": members_processed,
            "changed_count": changed_total,
            "eligible_problem_count": eligible_problem_count,
            "skipped_problem_count": skipped_problem_count,
            "system_admin_members": system_admin_members,
        },
    )
    msg = (
        f"synced contest problem access for {members_processed} member(s); "
        f"{changed_total} entry change(s) across {eligible_problem_count} writable contest problem(s)"
    )
    if skipped_problem_count > 0:
        msg += f"; {skipped_problem_count} unwritable contest problem(s) skipped"
    if system_admin_members > 0:
        msg += f"; {system_admin_members} system admin member(s) required no sync"
    return _contest_redirect(str(ctx["contest"]["slug"]), "access", message=msg)
