from __future__ import annotations

from fastapi import Form, HTTPException, Request

from app.impl.auth.shared import template_response
from app.impl.runtime.config import config
from app.impl.workspace.context_operation import audit, normalize_contest_title_required

from .shared import (
    _CONTEST_PROPERTY_DATE,
    _CONTEST_PROPERTY_LOCATION,
    _CONTEST_PROPERTY_SOURCE_MODE,
    _CONTEST_SOURCE_MODE_VALUES,
    _contest_ctx,
    _contest_redirect,
)


def contest_properties_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "properties")
    contest_id = int(ctx["contest"]["id"])
    props = config.contest_service.properties_map(contest_id)
    source_mode = props.get(_CONTEST_PROPERTY_SOURCE_MODE, "latest_committed")
    if source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        source_mode = "latest_committed"
    return template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "location": props.get(_CONTEST_PROPERTY_LOCATION),
            "date_text": props.get(_CONTEST_PROPERTY_DATE),
            "source_mode": source_mode,
        },
    )


def contest_properties_save(
    contest: str,
    user: str,
    title: str = Form(""),
    location: str = Form(""),
    date_text: str = Form(""),
    source_mode: str = Form("latest_committed"),
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
    safe_source_mode = source_mode.strip().lower()
    if safe_source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        safe_source_mode = "latest_committed"
    config.contest_service.update_title(contest_id, safe_title)
    config.contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_LOCATION, safe_location)
    config.contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_DATE, safe_date)
    config.contest_service.upsert_property(contest_id, actor_user_id, _CONTEST_PROPERTY_SOURCE_MODE, safe_source_mode)
    audit(
        actor_user_id,
        None,
        "contest.properties.save",
        {
            "contest_id": contest_id,
            "contest_slug": ctx["contest"]["slug"],
            "title": safe_title,
            "location": safe_location,
            "date": safe_date,
            "source_mode": safe_source_mode,
        },
    )
    return _contest_redirect(ctx["contest"]["slug"], user, "properties", message="contest properties saved")
