"""Synchronous Contest statement review pages."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.shared import _contest_ctx
from app.impl.runtime.dependency import runtime
from app.service.statement.html_render import (
    RESOURCE_PLACEHOLDER,
    number_statement_fragment,
)
from app.service.statement.latex_error import latex_failure_text
from app.service.statement.preview_state import StatementPreviewSource


def _source(value: str) -> StatementPreviewSource:
    if value not in {"workspace", "native_package"}:
        raise HTTPException(status_code=400, detail="invalid statement review source")
    return cast(StatementPreviewSource, value)


def _language(contest_id: int, value: str) -> str:
    language = runtime().contest_statement_service.resolve_language(
        contest_id,
        value,
    )
    if not language:
        raise HTTPException(status_code=400, detail="statement language is required")
    return language


def _require_roster_problem_read(ctx: dict[str, object]) -> None:
    contest = ctx["contest"]
    user = ctx["user"]
    if not isinstance(contest, dict) or not isinstance(user, dict):
        raise RuntimeError("invalid Contest page context")
    rows = runtime().contest_service.contest_problems(int(contest["id"]))
    access = runtime().access_query.problem_contexts(
        [int(row["problem_id"]) for row in rows],
        int(user["id"]),
    )
    blocked = [
        str(row["problem_slug"])
        for row in rows
        if not access[int(row["problem_id"])]["can_read"]
    ]
    if blocked:
        raise HTTPException(
            status_code=403,
            detail="problem read access required: " + ", ".join(blocked),
        )


def _review_items(
    contest_id: int,
    contest_slug: str,
    actor_user_id: int,
    preview,
) -> list[dict[str, object]]:
    roster = {
        row["problem_id"]: row
        for row in runtime().contest_service.contest_problems(contest_id)
    }
    items: list[dict[str, object]] = []
    for item in runtime().contest_statement_preview_service.items(preview):
        roster_item = roster.get(item["problem_id"])
        if roster_item is None:
            continue
        display_status = item["status"]
        display_error = item["error"]
        fragment = ""
        if display_status == "ok" and item["preview_id"]:
            fragment = runtime().statement_preview_service.html_fragment(
                item["preview_id"],
                actor_user_id=actor_user_id,
                problem_id=item["problem_id"],
            ) or ""
            resource_base = (
                f"/contests/{contest_slug}/statements/review/resources/"
                f"{item['preview_id']}/"
            )
            fragment = fragment.replace(RESOURCE_PLACEHOLDER, resource_base)
            try:
                fragment = number_statement_fragment(
                    fragment,
                    str(roster_item["idx"]),
                )
            except ValueError as exc:
                display_status = "failed"
                display_error = str(exc)
                fragment = ""
        pandoc_log = ""
        if display_status == "failed" and item["preview_id"]:
            pandoc_log = runtime().statement_preview_service.pandoc_log(
                item["preview_id"],
                actor_user_id=actor_user_id,
            )
        items.append(
            {
                **item,
                "status": display_status,
                "error": display_error,
                "idx": roster_item["idx"],
                "problem_slug": roster_item["problem_slug"],
                "fragment": fragment,
                "pandoc_log": pandoc_log,
            }
        )
    return items


def contest_statement_review_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "",
):
    ctx = _contest_ctx(contest, user, "overview", request=request)
    _require_roster_problem_read(ctx)
    contest_id = int(ctx["contest"]["id"])
    source_kind = _source(source)
    safe_language = _language(contest_id, language)
    localized_properties = runtime().contest_service.localized_properties_map(
        contest_id,
        safe_language,
    )
    preview = runtime().contest_statement_preview_service.build_html(
        contest_id,
        user_id=int(ctx["user"]["id"]),
        username=user,
        source_kind=source_kind,
        language=safe_language,
    )
    ctx["page_single_column"] = True
    return template_response(
        request,
        "contest_statement_review.html",
        {
            "ctx": ctx,
            "source": source_kind,
            "language": safe_language,
            "review_title": localized_properties.get(
                "title",
                str(ctx["contest"]["title"]),
            ),
            "preview": preview,
            "review_items": _review_items(
                contest_id,
                str(ctx["contest"]["slug"]),
                int(ctx["user"]["id"]),
                preview,
            ),
        },
    )


def contest_statement_review_build(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "",
):
    ctx = _contest_ctx(contest, user, "overview", request=request)
    _require_roster_problem_read(ctx)
    source_kind = _source(source)
    safe_language = _language(int(ctx["contest"]["id"]), language)
    runtime().contest_statement_preview_service.build_html(
        int(ctx["contest"]["id"]),
        user_id=int(ctx["user"]["id"]),
        username=user,
        source_kind=source_kind,
        language=safe_language,
    )
    target = (
        f"/contests/{ctx['contest']['slug']}/statements/review"
        f"?source={source_kind}&language={quote(safe_language)}"
    )
    return redirect_response(target, status_code=303)


def contest_statement_review_resource(
    request: Request,
    contest: str,
    preview_id: str,
    name: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "overview", request=request)
    _require_roster_problem_read(ctx)
    row = runtime().statement_preview_service.row(
        preview_id,
        actor_user_id=int(ctx["user"]["id"]),
    )
    if row is None or row["problem_id"] is None:
        raise HTTPException(status_code=404, detail="statement preview resource not found")
    problem_ids = {
        item["problem_id"]
        for item in runtime().contest_service.contest_problems(int(ctx["contest"]["id"]))
    }
    if row["problem_id"] not in problem_ids:
        raise HTTPException(status_code=404, detail="statement preview resource not found")
    access = runtime().access_query.problem_context(
        row["problem_id"],
        int(ctx["user"]["id"]),
    )
    if not access["can_read"]:
        raise HTTPException(status_code=403, detail=access["read_block_reason"])
    path = runtime().statement_preview_service.resource(
        preview_id,
        name,
        actor_user_id=int(ctx["user"]["id"]),
    )
    if path is None:
        raise HTTPException(status_code=404, detail="statement preview resource not found")
    return FileResponse(path)


def contest_statement_pdf_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "",
):
    ctx = _contest_ctx(contest, user, "overview", request=request)
    _require_roster_problem_read(ctx)
    contest_id = int(ctx["contest"]["id"])
    source_kind = _source(source)
    safe_language = _language(contest_id, language)
    preview = runtime().contest_statement_preview_service.build_pdf(
        contest_id,
        contest_slug=str(ctx["contest"]["slug"]),
        user_id=int(ctx["user"]["id"]),
        username=user,
        source_kind=source_kind,
        language=safe_language,
    )
    if preview["status"] != "ok":
        error = preview["summary"].get("error")
        detail = (
            error
            if isinstance(error, str) and error
            else "Contest statement PDF generation failed."
        )
        latex_log = runtime().statement_preview_service.latex_log(
            preview["id"],
            actor_user_id=int(ctx["user"]["id"]),
        )
        return PlainTextResponse(
            latex_failure_text(detail, latex_log),
            status_code=422,
        )
    path = runtime().statement_preview_service.pdf(
        preview["id"],
        actor_user_id=int(ctx["user"]["id"]),
    )
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="Contest statement PDF result is unavailable.",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{ctx['contest']['slug']}-{safe_language}-statements.pdf",
        content_disposition_type="inline",
    )
