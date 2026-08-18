"""On-demand Problem Statement HTML and PDF results."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import template_response
from app.impl.runtime.dependency import runtime
from app.service.statement.context import normalize_statement_language
from app.service.statement.html_render import RESOURCE_PLACEHOLDER
from app.service.statement.latex_error import latex_failure_text
from app.service.statement.preview_state import StatementPreviewSource


def _problem_access(problem: str, username: str):
    problem_row = runtime().workspace_service.problem_row(problem)
    user_row = runtime().workspace_service.user_row(username)
    access = runtime().access_query.problem_context(
        int(problem_row["id"]),
        int(user_row["id"]),
    )
    return problem_row, user_row, access


def _source(value: str) -> StatementPreviewSource:
    if value not in {"workspace", "native_package"}:
        raise HTTPException(status_code=400, detail="invalid statement preview source")
    return cast(StatementPreviewSource, value)


def _language(value: str) -> str:
    language = normalize_statement_language(value)
    if not language:
        raise HTTPException(status_code=400, detail="statement language is required")
    return language


def problem_statement_html_page(
    request: Request,
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "english",
):
    problem_row, user_row, access = _problem_access(problem, user)
    if not access["can_read"]:
        raise HTTPException(status_code=403, detail=access["read_block_reason"])
    source_kind = _source(source)
    safe_language = _language(language)
    allowed = access["can_write"] if source_kind == "workspace" else access["can_read"]
    if not allowed:
        reason = (
            access["write_block_reason"]
            if source_kind == "workspace"
            else access["read_block_reason"]
        )
        raise HTTPException(status_code=403, detail=reason)
    row = runtime().statement_preview_service.build_problem(
        problem,
        user,
        source_kind=source_kind,
        output_kind="html",
        language=safe_language,
    )
    fragment = ""
    pandoc_log = ""
    if row is not None and row["status"] == "ok":
        fragment = runtime().statement_preview_service.html_fragment(
            row["id"],
            actor_user_id=int(user_row["id"]),
            problem_id=int(problem_row["id"]),
        ) or ""
        resource_base = (
            f"/problems/{problem}/statement/html/resources/{row['id']}/"
        )
        fragment = fragment.replace(RESOURCE_PLACEHOLDER, resource_base)
    elif row is not None and row["status"] == "failed":
        pandoc_log = runtime().statement_preview_service.pandoc_log(
            row["id"],
            actor_user_id=int(user_row["id"]),
        )
    return template_response(
        request,
        "statement_html_preview.html",
        {
            "user": user_row,
            "active_main": "problems",
            "problem": problem_row,
            "source": source_kind,
            "language": safe_language,
            "preview": row,
            "pandoc_log": pandoc_log,
            "statement_fragment": fragment,
            "statement_href": _statement_href(
                problem,
                safe_language,
                contest=request.query_params.get("contest", ""),
            ),
        },
    )


def problem_statement_pdf_page(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "english",
):
    problem_row, user_row, access = _problem_access(problem, user)
    if not access["can_read"]:
        raise HTTPException(status_code=403, detail=access["read_block_reason"])
    source_kind = _source(source)
    allowed = access["can_write"] if source_kind == "workspace" else access["can_read"]
    if not allowed:
        reason = (
            access["write_block_reason"]
            if source_kind == "workspace"
            else access["read_block_reason"]
        )
        raise HTTPException(status_code=403, detail=reason)
    safe_language = _language(language)
    row = runtime().statement_preview_service.build_problem(
        problem,
        user,
        source_kind=source_kind,
        output_kind="pdf",
        language=safe_language,
    )
    if row["status"] != "ok":
        error = row["summary"].get("error")
        detail = (
            error
            if isinstance(error, str) and error
            else "Statement PDF generation failed."
        )
        latex_log = runtime().statement_preview_service.latex_log(
            row["id"],
            actor_user_id=int(user_row["id"]),
        )
        return PlainTextResponse(
            latex_failure_text(detail, latex_log),
            status_code=422,
        )
    path = runtime().statement_preview_service.pdf(
        row["id"],
        actor_user_id=int(user_row["id"]),
    )
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="Statement PDF result is unavailable.",
        )
    return FileResponse(
        Path(path),
        media_type="application/pdf",
        filename=f"statement-{safe_language}.pdf",
        content_disposition_type="inline",
    )


def _statement_href(problem: str, language: str, *, contest: str) -> str:
    query = {"language": language}
    if contest:
        query["contest"] = contest
    return f"/problems/{problem}/statement?{urlencode(query)}"


def problem_statement_html_resource(
    problem: str,
    preview_id: str,
    name: str,
    user: Annotated[str, Depends(require_session_user)],
):
    problem_row, user_row, access = _problem_access(problem, user)
    if not access["can_read"]:
        raise HTTPException(status_code=403, detail=access["read_block_reason"])
    row = runtime().statement_preview_service.row(
        preview_id,
        actor_user_id=int(user_row["id"]),
    )
    if row is None or row["problem_id"] != int(problem_row["id"]):
        raise HTTPException(status_code=404, detail="statement preview resource not found")
    path = runtime().statement_preview_service.resource(
        preview_id,
        name,
        actor_user_id=int(user_row["id"]),
    )
    if path is None:
        raise HTTPException(status_code=404, detail="statement preview resource not found")
    return FileResponse(path)
