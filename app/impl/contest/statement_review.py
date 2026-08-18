"""Synchronous Contest statement review pages."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.contest.shared import _contest_ctx
from app.impl.runtime.dependency import runtime
from app.service.disk.statement_preview_store import StatementPreviewSource
from app.service.statement.context import normalize_statement_language
from app.service.statement.html_render import RESOURCE_PLACEHOLDER


def _source(value: str) -> StatementPreviewSource:
    if value not in {"workspace", "native_package"}:
        raise HTTPException(status_code=400, detail="invalid statement review source")
    return cast(StatementPreviewSource, value)


def _language(value: str) -> str:
    language = normalize_statement_language(value)
    if not language:
        raise HTTPException(status_code=400, detail="statement language is required")
    return language


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
        fragment = ""
        if item["status"] == "ok" and item["preview_id"]:
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
        items.append(
            {
                **item,
                "idx": roster_item["idx"],
                "problem_slug": roster_item["problem_slug"],
                "fragment": fragment,
            }
        )
    return items


def contest_statement_review_page(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "english",
    preview_id: str = "",
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    contest_id = int(ctx["contest"]["id"])
    source_kind = _source(source)
    safe_language = _language(language)
    preview = None
    if preview_id:
        candidate = runtime().statement_preview_service.row(
            preview_id,
            actor_user_id=int(ctx["user"]["id"]),
        )
        if (
            candidate is not None
            and candidate["contest_id"] == contest_id
            and candidate["source_kind"] == source_kind
            and candidate["output_kind"] == "html"
            and candidate["language"] == safe_language
        ):
            preview = candidate
    if preview is None:
        preview = runtime().contest_statement_preview_service.latest_html(
            contest_id,
            actor_user_id=int(ctx["user"]["id"]),
            source_kind=source_kind,
            language=safe_language,
        )
    return template_response(
        request,
        "contest_statement_review.html",
        {
            "ctx": ctx,
            "source": source_kind,
            "language": safe_language,
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
    language: str = "english",
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    if not ctx["access"]["can_build"]:
        raise HTTPException(
            status_code=403,
            detail=ctx["access"]["build_block_reason"],
        )
    source_kind = _source(source)
    safe_language = _language(language)
    preview = runtime().contest_statement_preview_service.build_html(
        int(ctx["contest"]["id"]),
        user_id=int(ctx["user"]["id"]),
        username=user,
        source_kind=source_kind,
        language=safe_language,
    )
    target = (
        f"/contests/{ctx['contest']['slug']}/statements/review"
        f"?source={source_kind}&language={quote(safe_language)}"
        f"&preview_id={quote(preview['id'])}"
    )
    return redirect_response(target, status_code=303)


def contest_statement_review_resource(
    request: Request,
    contest: str,
    preview_id: str,
    name: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
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
    language: str = "english",
    preview_id: str = "",
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    contest_id = int(ctx["contest"]["id"])
    source_kind = _source(source)
    safe_language = _language(language)
    preview = None
    if preview_id:
        candidate = runtime().statement_preview_service.row(
            preview_id,
            actor_user_id=int(ctx["user"]["id"]),
        )
        if (
            candidate is not None
            and candidate["contest_id"] == contest_id
            and candidate["source_kind"] == source_kind
            and candidate["output_kind"] == "pdf"
            and candidate["language"] == safe_language
        ):
            preview = candidate
    if preview is None:
        preview = runtime().contest_statement_preview_service.latest_pdf(
            contest_id,
            actor_user_id=int(ctx["user"]["id"]),
            source_kind=source_kind,
            language=safe_language,
        )
    pdf_available = bool(
        preview is not None
        and preview["status"] == "ok"
        and runtime().statement_preview_service.pdf(
            preview["id"],
            actor_user_id=int(ctx["user"]["id"]),
        )
        is not None
    )
    return template_response(
        request,
        "contest_statement_pdf.html",
        {
            "ctx": ctx,
            "source": source_kind,
            "language": safe_language,
            "preview": preview,
            "pdf_available": pdf_available,
        },
    )


def contest_statement_pdf_build(
    request: Request,
    contest: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "english",
    insert_blank_pages: bool = False,
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    if not ctx["access"]["can_build"]:
        raise HTTPException(status_code=403, detail=ctx["access"]["build_block_reason"])
    source_kind = _source(source)
    safe_language = _language(language)
    preview = runtime().contest_statement_preview_service.build_pdf(
        int(ctx["contest"]["id"]),
        contest_slug=str(ctx["contest"]["slug"]),
        user_id=int(ctx["user"]["id"]),
        username=user,
        source_kind=source_kind,
        language=safe_language,
        insert_blank_pages=bool(insert_blank_pages),
    )
    target = (
        f"/contests/{ctx['contest']['slug']}/statements/pdf"
        f"?source={source_kind}&language={quote(safe_language)}"
        f"&preview_id={quote(preview['id'])}"
    )
    return redirect_response(target, status_code=303)


def contest_statement_pdf_file(
    request: Request,
    contest: str,
    preview_id: str,
    user: Annotated[str, Depends(require_session_user)],
):
    ctx = _contest_ctx(contest, user, "packages", request=request)
    row = runtime().statement_preview_service.row(
        preview_id,
        actor_user_id=int(ctx["user"]["id"]),
    )
    if row is None or row["contest_id"] != int(ctx["contest"]["id"]):
        raise HTTPException(status_code=404, detail="Contest statement PDF not found")
    path = runtime().statement_preview_service.pdf(
        preview_id,
        actor_user_id=int(ctx["user"]["id"]),
    )
    if path is None:
        raise HTTPException(status_code=404, detail="Contest statement PDF not found")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{ctx['contest']['slug']}-{row['language']}-statements.pdf",
    )
