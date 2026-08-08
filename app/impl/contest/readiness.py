from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.shared import _contest_ctx, _contest_problem_rows


def contest_readiness_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
):
    """Report immutable published/materialized revisions without provisioning workspaces."""

    ctx = _contest_ctx(contest, user, "readiness")
    rows = _contest_problem_rows(
        int(ctx["contest"]["id"]),
        str(ctx["user"]["username"]),
        int(ctx["user"]["id"]),
    )
    return template_response(
        request,
        "contest_readiness.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "ready_count": sum(1 for row in rows if row["materialization_id"]),
            "current_count": sum(1 for row in rows if row["current_is_materialized"]),
        },
    )
