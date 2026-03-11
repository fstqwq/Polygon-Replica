from __future__ import annotations

from fastapi import Form, HTTPException, Request

from app.impl.auth.public import template_response
from app.impl.runtime.config import config
from app.impl.workspace.public import audit, normalize_contest_title_required

from .shared import (
    _CONTEST_PROPERTY_DATE,
    _CONTEST_PROPERTY_LOCATION,
    _CONTEST_PROPERTY_SOURCE_MODE,
    _CONTEST_SOURCE_MODE_VALUES,
    _contest_ctx,
    _contest_properties_map,
    _contest_redirect,
    _upsert_contest_property,
)
def contest_properties_page(request: Request, contest: str, user: str):
    ctx = _contest_ctx(contest, user, "properties")
    contest_id = int(ctx["contest"]["id"])
    props = _contest_properties_map(contest_id)
    source_mode = str(props.get(_CONTEST_PROPERTY_SOURCE_MODE) or "latest_committed").strip()
    if source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        source_mode = "latest_committed"
    return template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "location": str(props.get(_CONTEST_PROPERTY_LOCATION) or ""),
            "date_text": str(props.get(_CONTEST_PROPERTY_DATE) or ""),
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
        raise HTTPException(status_code=403, detail=str(ctx["access"].get("write_block_reason") or "write access required"))
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    current_title = str(ctx["contest"]["title"] or "").strip()
    safe_title = normalize_contest_title_required(str(title or "").strip() or current_title)
    safe_location = str(location or "").strip()
    safe_date = str(date_text or "").strip()
    safe_source_mode = str(source_mode or "").strip().lower()
    if safe_source_mode not in _CONTEST_SOURCE_MODE_VALUES:
        safe_source_mode = "latest_committed"
    config.db.execute(
        "UPDATE contests SET title=? WHERE id=?",
        [safe_title, contest_id],
    )
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_LOCATION, safe_location)
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_DATE, safe_date)
    _upsert_contest_property(contest_id, actor_user_id, _CONTEST_PROPERTY_SOURCE_MODE, safe_source_mode)
    audit(
        actor_user_id,
        None,
        "contest.properties.save",
        {
            "contest_id": contest_id,
            "contest_slug": str(ctx["contest"]["slug"]),
            "title": safe_title,
            "location": safe_location,
            "date": safe_date,
            "source_mode": safe_source_mode,
        },
    )
    return _contest_redirect(str(ctx["contest"]["slug"]), user, "properties", message="contest properties saved")


