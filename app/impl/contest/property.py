from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.shared import (
    _CONTEST_PROPERTY_DATE,
    _CONTEST_PROPERTY_LOCATION,
    _contest_ctx,
    _contest_redirect,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import normalize_contest_title_required
from app.service.statement.context import normalize_statement_language


def contest_properties_page(request: Request, contest: str, user: Annotated[str, Depends(require_session_user)]):
    ctx = _contest_ctx(contest, user, "properties", request=request)
    contest_id = int(ctx["contest"]["id"])
    props = runtime().contest_service.properties_map(contest_id)
    statement_language = runtime().contest_service.statement_default_language(
        contest_id
    )
    return template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "location": props.get(_CONTEST_PROPERTY_LOCATION),
            "date_text": props.get(_CONTEST_PROPERTY_DATE),
            "statement_language": statement_language,
        },
    )


def contest_properties_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    title: str = Form(""),
    location: str = Form(""),
    date_text: str = Form(""),
    statement_language: str = Form(""),
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
    current_language = runtime().contest_service.statement_default_language(contest_id)
    safe_language = normalize_statement_language(
        statement_language or current_language
    )
    if not safe_language:
        raise HTTPException(
            status_code=400,
            detail="statement language is required",
        )
    runtime().contest_service.update_title(contest_id, safe_title)
    runtime().contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_LOCATION, safe_location)
    runtime().contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_DATE, safe_date)
    runtime().contest_service.set_statement_default_language(
        contest_id,
        actor_user_id,
        safe_language,
    )
    return _contest_redirect(ctx["contest"]["slug"], "properties", message="contest properties saved")
