from app.impl.auth.session import require_session_user
from typing import Annotated

from fastapi import Request, Depends

from app.impl.auth.shared import template_response
from app.impl.contest.problem_rows import contest_overview_problem_rows
from app.impl.contest.workspace_scope import add_contest_problem_hrefs

from app.impl.contest.shared import _contest_ctx


def contest_overview_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "overview")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = add_contest_problem_hrefs(
        request,
        contest_slug=str(ctx["contest"]["slug"]),
        rows=contest_overview_problem_rows(
            contest_id,
            str(ctx["user"]["username"]),
            user_id,
        ),
    )
    owner_prefix_chars = max(
        (len(str(row["slug_owner"])) + 1 for row in rows),
        default=0,
    )
    package_ready_count = sum(
        1
        for row in rows
        if row["readiness"] is not None
        and row["readiness"]["package"]["state"] == "ready"
    )
    package_stale_count = sum(
        1
        for row in rows
        if row["readiness"] is not None
        and row["readiness"]["package"]["state"] == "stale"
    )
    package_none_count = len(rows) - package_ready_count - package_stale_count
    return template_response(
        request,
        "contest_overview.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "owner_prefix_chars": owner_prefix_chars,
            "package_ready_count": package_ready_count,
            "package_stale_count": package_stale_count,
            "package_none_count": package_none_count,
        },
    )
