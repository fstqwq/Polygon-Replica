from __future__ import annotations

from fastapi import Request

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config

from .shared import _contest_ctx, _contest_problem_rows


def contest_overview_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "overview")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = _contest_problem_rows(contest_id, str(ctx["user"]["username"]), user_id)
    return template_response(
        request,
        "contest_overview.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "problem_count": len(rows),
            "member_count": config.contest_service.member_count(contest_id),
            "owner_count": config.contest_service.owner_count(contest_id),
            "latest_job": config.contest_service.latest_job(contest_id),
            "contest_properties": config.contest_service.properties_map(contest_id),
        },
    )
