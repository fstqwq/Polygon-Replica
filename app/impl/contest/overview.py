from __future__ import annotations

from fastapi import Request

from app.impl.auth.public import template_response
from app.impl.runtime.config import config

from .shared import _contest_ctx, _contest_owner_count, _contest_problem_rows, _contest_properties_map
def contest_overview_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "overview")
    contest_id = int(ctx["contest"]["id"])
    user_id = int(ctx["user"]["id"])
    rows = _contest_problem_rows(contest_id, str(ctx["user"]["username"]), user_id)
    members_row = config.db.fetch_one(
        "SELECT COUNT(*) AS c FROM contest_members WHERE contest_id=?",
        [contest_id],
    )
    member_count = int(members_row["c"] or 0) if members_row is not None else 0
    latest_job = config.db.fetch_one(
        """
        SELECT id,job_type,status,created_at,finished_at
        FROM contest_jobs
        WHERE contest_id=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        [contest_id],
    )
    props = _contest_properties_map(contest_id)
    return template_response(
        request,
        "contest_overview.html",
        {
            "ctx": ctx,
            "problem_rows": rows,
            "problem_count": len(rows),
            "member_count": member_count,
            "owner_count": _contest_owner_count(contest_id),
            "latest_job": dict(latest_job) if latest_job is not None else None,
            "contest_properties": props,
        },
    )


