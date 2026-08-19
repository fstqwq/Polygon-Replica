from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.contest.shared import (
    _contest_ctx,
    _contest_redirect,
)
from app.impl.contest.statement_source import (
    contest_statement_source_context,
    statement_review_languages,
)
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_operation import normalize_contest_title_required
from app.service.contest.property import (
    BANNER_PROPERTY,
    DEFAULT_CONTEST_BANNER,
    INSERT_BLANK_PAGE_PROPERTY,
    REQUIRED_CONTEST_PROPERTY_KEYS,
    contest_property_language,
    contest_property_is_deletable,
    localized_contest_property_key,
    normalize_contest_property_key,
)


def _contest_property_table(
    properties: dict[str, str],
) -> dict[str, object]:
    displayed_properties = dict(properties)

    preferred_bases = {
        "title": 0,
        "location": 1,
        "date": 2,
        BANNER_PROPERTY: 3,
        INSERT_BLANK_PAGE_PROPERTY: 4,
    }

    def property_sort_key(key: str) -> tuple[int, str, int, str]:
        base_key, separator, language = key.partition(".")
        return (
            preferred_bases.get(base_key, len(preferred_bases)),
            base_key,
            1 if separator else 0,
            language,
        )

    grouped: dict[str, list[dict[str, object]]] = {}
    for key in sorted(displayed_properties, key=property_sort_key):
        language = contest_property_language(key)
        base_key = key.partition(".")[0]
        grouped.setdefault(base_key, []).append(
            {
                "key": key,
                "value": displayed_properties[key],
                "editor_rows": min(
                    8,
                    max(1, displayed_properties[key].count("\n") + 1),
                ),
                "scope_label": language.title() if language else "All",
                "required": key == "title",
                "persisted": key in properties,
            }
        )

    groups: list[dict[str, object]] = []
    for base_key, values in grouped.items():
        edit_values = list(values)
        if not any(str(row["key"]) == base_key for row in edit_values):
            edit_values.insert(
                0,
                {
                    "key": base_key,
                    "value": "",
                    "editor_rows": 1,
                    "scope_label": "All",
                    "required": base_key == "title",
                    "persisted": False,
                },
            )
        groups.append(
            {
                "key": base_key,
                "values": values,
                "edit_values": edit_values,
                "existing_keys": [
                    str(row["key"])
                    for row in values
                    if bool(row["persisted"])
                ],
                "boolean": base_key == INSERT_BLANK_PAGE_PROPERTY,
                "deletable": contest_property_is_deletable(base_key),
                "localizable": base_key != INSERT_BLANK_PAGE_PROPERTY,
                "persisted": any(bool(row["persisted"]) for row in values),
                "kind_label": (
                    "Required"
                    if base_key in REQUIRED_CONTEST_PROPERTY_KEYS
                    else ""
                ),
                "popup_id": f"contest-property-edit-{base_key.replace('_', '-')}",
            }
        )
    return {
        "groups": groups,
        "can_insert_banner": BANNER_PROPERTY not in properties,
        "can_insert_blank_page": INSERT_BLANK_PAGE_PROPERTY not in properties,
    }


def contest_properties_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    language: str = "",
    source_path: str = "",
    scope: str = "",
):
    ctx = _contest_ctx(contest, user, "properties", request=request)
    contest_id = int(ctx["contest"]["id"])
    props = runtime().contest_service.properties_map(contest_id)
    source_context = contest_statement_source_context(
        contest_id=contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        language=language,
        source_path=source_path,
        scope=scope,
        additional_languages=statement_review_languages(ctx),
    )
    return template_response(
        request,
        "contest_properties.html",
        {
            "ctx": ctx,
            "contest_property_table": _contest_property_table(props),
            **source_context,
        },
    )


def contest_properties_save(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    property_keys: list[str] = Form([]),
    property_values: list[str] = Form([]),
    existing_property_keys: list[str] = Form([]),
):
    ctx = _contest_ctx(contest, user, "properties")
    if not bool(ctx["access"].get("can_write")):
        reason = ctx["access"].get("write_block_reason")
        if not isinstance(reason, str) or not reason:
            reason = "write access required"
        raise HTTPException(status_code=403, detail=reason)
    contest_id = int(ctx["contest"]["id"])
    actor_user_id = int(ctx["user"]["id"])
    if len(property_keys) != len(property_values):
        raise HTTPException(status_code=400, detail="invalid contest property form")
    values: dict[str, object] = {}
    for key, value in zip(property_keys, property_values, strict=True):
        raw_key = str(key).strip()
        raw_value = str(value).strip()
        if not raw_key and not raw_value:
            continue
        if not raw_key:
            raise HTTPException(status_code=400, detail="contest property key is required")
        try:
            safe_key = normalize_contest_property_key(raw_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if safe_key in values:
            raise HTTPException(status_code=400, detail="duplicate contest property")
        values[safe_key] = raw_value
    for existing_key in existing_property_keys:
        try:
            safe_existing_key = normalize_contest_property_key(existing_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if safe_existing_key not in values:
            values[safe_existing_key] = None
    if "title" in values:
        title = str(values.get("title") or "").strip()
        values["title"] = normalize_contest_title_required(title)
    try:
        runtime().contest_service.set_properties(
            contest_id,
            actor_user_id,
            values,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _contest_redirect(
        ctx["contest"]["slug"],
        "properties",
        message="contest properties saved",
    )


def contest_property_add(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    property_key: Annotated[str, Form()] = "",
    property_value: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    _require_contest_property_write(ctx)
    try:
        safe_key = normalize_contest_property_key(property_key)
        if "." in safe_key:
            raise ValueError("add a base property before adding language overrides")
        safe_value = property_value.strip()
        if not safe_value:
            raise ValueError("contest property value is required")
        existing = runtime().contest_service.properties_map(int(ctx["contest"]["id"]))
        if safe_key in existing:
            raise ValueError(f"contest property already exists: {safe_key}")
        runtime().contest_service.set_properties(
            int(ctx["contest"]["id"]),
            int(ctx["user"]["id"]),
            {safe_key: safe_value},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        message="contest property added",
    )


def contest_property_language_add(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    property_key: Annotated[str, Form()] = "",
    property_language: Annotated[str, Form()] = "",
    property_value: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    _require_contest_property_write(ctx)
    try:
        safe_key = localized_contest_property_key(property_key, property_language)
        safe_value = property_value.strip()
        if not safe_value:
            raise ValueError("contest property language value is required")
        existing = runtime().contest_service.properties_map(int(ctx["contest"]["id"]))
        if safe_key in existing:
            raise ValueError(f"contest property language already exists: {safe_key}")
        runtime().contest_service.set_properties(
            int(ctx["contest"]["id"]),
            int(ctx["user"]["id"]),
            {safe_key: safe_value},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        message="contest property language added",
    )


def contest_property_delete(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    property_key: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    _require_contest_property_write(ctx)
    try:
        safe_key = normalize_contest_property_key(property_key)
        properties = runtime().contest_service.properties_map(int(ctx["contest"]["id"]))
        if "." in safe_key:
            removals = {safe_key: None}
        else:
            if not contest_property_is_deletable(safe_key):
                raise ValueError(f"contest property cannot be deleted: {safe_key}")
            prefix = f"{safe_key}."
            removals = {
                key: None
                for key in properties
                if key == safe_key or key.startswith(prefix)
            }
        runtime().contest_service.set_properties(
            int(ctx["contest"]["id"]),
            int(ctx["user"]["id"]),
            removals,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        message=(
            "contest property language deleted"
            if "." in safe_key
            else "contest property deleted"
        ),
    )


def contest_property_insert_preset(
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    property_key: Annotated[str, Form()] = "",
):
    ctx = _contest_ctx(contest, user, "properties")
    _require_contest_property_write(ctx)
    presets: dict[str, object] = {
        BANNER_PROPERTY: DEFAULT_CONTEST_BANNER,
        INSERT_BLANK_PAGE_PROPERTY: True,
    }
    if property_key not in presets:
        raise HTTPException(status_code=400, detail="unknown contest property preset")
    contest_id = int(ctx["contest"]["id"])
    properties = runtime().contest_service.properties_map(contest_id)
    if property_key == BANNER_PROPERTY and BANNER_PROPERTY in properties:
        raise HTTPException(status_code=409, detail="contest banner already exists")
    runtime().contest_service.set_properties(
        contest_id,
        int(ctx["user"]["id"]),
        {property_key: presets[property_key]},
    )
    return _contest_redirect(
        str(ctx["contest"]["slug"]),
        "properties",
        message="contest property inserted",
    )


def _require_contest_property_write(ctx: dict[str, object]) -> None:
    access = ctx["access"]
    assert isinstance(access, dict)
    if bool(access.get("can_write")):
        return
    reason = access.get("write_block_reason")
    raise HTTPException(
        status_code=403,
        detail=str(reason) if reason else "write access required",
    )
