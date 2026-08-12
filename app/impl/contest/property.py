from __future__ import annotations
from app.impl.auth.session import require_session_user
from typing import Annotated

from fastapi import Form, HTTPException, Request, Depends

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import normalize_contest_title_required

from app.impl.contest.shared import (
    _CONTEST_PROPERTY_DATE,
    _CONTEST_PROPERTY_LOCATION,
    _contest_ctx,
    _contest_redirect,
)


def contest_properties_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "properties")
    contest_id = int(ctx["contest"]["id"])
    props = config.contest_service.properties_map(contest_id)
    return template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "location": props.get(_CONTEST_PROPERTY_LOCATION),
            "date_text": props.get(_CONTEST_PROPERTY_DATE),
            "statement_language": config.contest_service.statement_default_language(contest_id),
        },
    )


def contest_properties_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    title: str = Form(""),
    location: str = Form(""),
    date_text: str = Form(""),
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        reason = ctx["access"].get("write_block_reason")
        if not isinstance(reason, str) or not reason:
            reason = "write access required"
        raise HTTPException(status_code=403, detail=reason)
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    current_title = ctx["contest"]["title"]
    safe_title = normalize_contest_title_required(title.strip() or current_title)
    safe_location = location.strip()
    safe_date = date_text.strip()
    config.contest_service.update_title(contest_id, safe_title)
    config.contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_LOCATION, safe_location)
    config.contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_DATE, safe_date)
    return _contest_redirect(ctx["contest"]["slug"], "properties", message="contest properties saved")
