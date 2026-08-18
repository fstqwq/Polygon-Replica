from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException, Request

from app.impl.auth.shared import redirect_response
from app.impl.contest.workspace_scope import contest_workspace_context_for_contest_page
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context import global_user_ctx
from app.impl.workspace.context_operation import normalize_contest_slug_required
from app.impl.workspace.problem_config import read_problem_config
from app.service.platform.git_process import run_git
from app.service.problem.runtime_config import (
    ProblemConfig, dumps_problem_config, problem_config_limits,
)


_CONTEST_PROPERTY_LOCATION = "location"
_CONTEST_PROPERTY_DATE = "date"


def _contest_nav(contest_slug: str, active: str) -> list[dict[str, str | bool]]:
    base = f"/contests/{contest_slug}"
    return [
        {"key": "problems", "label": "Problems", "href": f"{base}/overview", "active": active == "overview"},
        {
            "key": "properties",
            "label": "Properties",
            "href": f"{base}/properties",
            "active": active == "properties",
        },
        {
            "key": "packages",
            "label": "Statements & Builds",
            "href": f"{base}/packages",
            "active": active == "packages",
        },
    ]


def _contest_ctx(
    contest_slug: str,
    user: str,
    active_page: str,
    *,
    request: Request | None = None,
) -> dict:
    gctx = global_user_ctx(user)
    safe_slug = normalize_contest_slug_required(contest_slug)
    contest_row = runtime().contest_service.contest_context(safe_slug)
    if contest_row is None:
        raise HTTPException(status_code=404, detail="contest not found")
    access = runtime().access_query.contest_context(
        int(contest_row["id"]),
        int(gctx["user"]["id"]),
    )
    if not access.get("can_read"):
        read_block_reason = access.get("read_block_reason")
        raise HTTPException(
            status_code=403,
            detail=str(read_block_reason) if read_block_reason is not None else "contest access required",
        )
    context = {
        "user": gctx["user"],
        "contest": {
            "id": int(contest_row["id"]),
            "slug": str(contest_row["slug"]),
            "title": str(contest_row["title"]),
            "owner_user_id": int(contest_row["owner_user_id"]),
            "status": str(contest_row["status"]),
            "source_generation": int(contest_row["source_generation"]),
            "location": str(contest_row["location"]),
            "date": str(contest_row["date"]),
            "statement_default_language": str(contest_row["statement_default_language"]),
            "created_at": contest_row["created_at"],
        },
        "access": access,
        "active_main": "contests",
        "contest_nav": _contest_nav(str(contest_row["slug"]), active_page),
        "contest_access_href": f"/contests/{contest_row['slug']}/access",
        "contest_access_active": active_page == "access",
        "statement_review_link_groups": [],
    }
    if request is not None:
        context["contest_workspace"] = contest_workspace_context_for_contest_page(
            request,
            contest_id=int(contest_row["id"]),
            contest_slug=str(contest_row["slug"]),
            contest_title=str(contest_row["title"]),
            user_id=int(gctx["user"]["id"]),
        )
        link_groups = runtime().contest_statement_preview_service.link_groups(
            int(contest_row["id"]),
            user_id=int(gctx["user"]["id"]),
            username=user,
        )
        context["statement_review_link_groups"] = [
            {
                **group,
                "links": [
                    {
                        "label": f"Review Statements ({language.title()})",
                        "language": language,
                        "href": (
                            f"/contests/{contest_row['slug']}/statements/review"
                            f"?source={group['source']}&language={quote(language)}"
                        ),
                        "pdf_href": (
                            f"/contests/{contest_row['slug']}/statements/pdf"
                            f"?source={group['source']}&language={quote(language)}"
                        ),
                    }
                    for language in group["languages"]
                ],
            }
            for group in link_groups
        ]
    return context



def _contest_redirect(
    contest_slug: str,
    page: str,
    *,
    query: str = "",
    fragment: str = "",
    message: str = "",
):
    target = f"/contests/{contest_slug}/{page}"
    if query:
        target += f"?{query}"
    if fragment:
        target += f"#{fragment}"
    return redirect_response(target, status_code=303, message=message)

def _problem_general_payload_map(
    problem_ids: list[str],
    time_limit_ms_values: list[str],
    memory_limit_mb_values: list[str],
) -> dict[int, dict[str, object]]:
    result: dict[int, dict[str, object]] = {}
    for index, raw_pid in enumerate(list(problem_ids or [])):
        try:
            pid = int(str(raw_pid or "").strip())
        except Exception:
            continue
        if pid <= 0:
            continue
        tl_value = str(time_limit_ms_values[index] if index < len(time_limit_ms_values) else "").strip()
        ml_value = str(memory_limit_mb_values[index] if index < len(memory_limit_mb_values) else "").strip()
        result[pid] = {"time_limit_ms": tl_value, "memory_limit_mb": ml_value}
    return result

def _run_problem_general_update(
    *,
    contest_slug: str,
    actor_username: str,
    actor_user_id: int,
    problem_id: int,
    problem_slug: str,
    requested_time_limit_ms: str,
    requested_memory_limit_mb: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "problem_id": int(problem_id),
        "problem_slug": str(problem_slug),
        "requested": {
            "time_limit_ms": requested_time_limit_ms.strip(),
            "memory_limit_mb": requested_memory_limit_mb.strip(),
        },
        "status": "failed",
        "commit_id": "",
        "error": "",
    }
    problem_access = runtime().access_query.problem_context(
        problem_id,
        actor_user_id,
    )
    if not problem_access["can_write"]:
        result["error"] = problem_access["write_block_reason"]
        return result
    try:
        workspace = Path(runtime().workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True))
        limits = problem_config_limits(runtime().config_values)
        try:
            safe_tl = int(requested_time_limit_ms)
            safe_ml = int(requested_memory_limit_mb)
        except ValueError as exc:
            raise ValueError("problem limits must be integers") from exc
        if not limits.min_time_limit_ms <= safe_tl <= limits.max_time_limit_ms:
            raise ValueError("time limit is outside the configured range")
        if not limits.min_memory_limit_mb <= safe_ml <= limits.max_memory_limit_mb:
            raise ValueError("memory limit is outside the configured range")
        with runtime().workspace_service.workspace_lock(workspace):
            has_head = run_git(["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"]).returncode == 0
            if not has_head:
                raise RuntimeError("bulk TL/ML update requires an initialized repository; create the initial commit first")
            before = runtime().git_service.status_change_summary(workspace, limit=1)
            if int(before.get("total", 0)) > 0:
                raise RuntimeError("workspace has uncommitted changes")
            _payload, general_cfg, cfg_path = read_problem_config(workspace)
            payload = ProblemConfig(
                time_limit_ms=safe_tl,
                memory_limit_mb=safe_ml,
                mode=general_cfg["mode"],
                pass_limit=general_cfg["pass_limit"],
            )
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                dumps_problem_config(payload, limits=limits), encoding="utf-8", newline="\n"
            )
            after = runtime().git_service.status_change_summary(workspace, limit=1)
            if int(after.get("total", 0)) <= 0:
                result["status"] = "skipped"
                return result
            commit_msg = f"contest {contest_slug}: bulk update TL/ML"
            commit_id = runtime().git_service.commit(
                workspace,
                commit_msg,
                actor_username,
                f"{actor_username}@polygonlike.local",
            )
            try:
                runtime().git_service.push(workspace, "main")
            except Exception as exc:
                try:
                    runtime().git_service.rollback_last_commit(workspace, expected_head=commit_id)
                except Exception as rollback_exc:
                    raise RuntimeError(f"push failed: {exc}; rollback failed: {rollback_exc}") from exc
                raise RuntimeError(f"push failed: {exc}; commit rolled back") from exc
            result["status"] = "success"
            result["commit_id"] = str(commit_id)
        try:
            runtime().workspace_service.ensure_workspace(problem_slug, actor_username, refresh_status=True)
        except Exception:
            pass
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
