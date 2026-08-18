"""Read-only statement HTML preview pages and explicit build actions."""

from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.impl.auth.session import require_session_user
from app.impl.auth.shared import redirect_response, template_response
from app.impl.runtime.dependency import runtime
from app.service.statement.context import normalize_statement_language
from app.service.statement.html_render import RESOURCE_PLACEHOLDER
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
    preview_id: str = "",
):
    problem_row, user_row, access = _problem_access(problem, user)
    if not access["can_read"]:
        raise HTTPException(status_code=403, detail=access["read_block_reason"])
    source_kind = _source(source)
    safe_language = _language(language)
    row = None
    if preview_id:
        candidate = runtime().statement_preview_service.row(
            preview_id,
            actor_user_id=int(user_row["id"]),
        )
        if (
            candidate is not None
            and candidate["problem_id"] == int(problem_row["id"])
            and candidate["source_kind"] == source_kind
            and candidate["output_kind"] == "html"
            and candidate["language"] == safe_language
        ):
            row = candidate
    if row is None:
        row = runtime().statement_preview_service.latest_problem(
            int(problem_row["id"]),
            actor_user_id=int(user_row["id"]),
            source_kind=source_kind,
            output_kind="html",
            language=safe_language,
        )
    fragment = ""
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
            "statement_fragment": fragment,
            "can_build": access["can_write"] if source_kind == "workspace" else access["can_read"],
        },
    )


def problem_statement_html_build(
    problem: str,
    user: Annotated[str, Depends(require_session_user)],
    source: str = "workspace",
    language: str = "english",
):
    problem_row, _user_row, access = _problem_access(problem, user)
    source_kind = _source(source)
    allowed = access["can_write"] if source_kind == "workspace" else access["can_read"]
    if not allowed:
        reason = access["write_block_reason"] if source_kind == "workspace" else access["read_block_reason"]
        raise HTTPException(status_code=403, detail=reason)
    safe_language = _language(language)
    row = runtime().statement_preview_service.build_problem(
        problem,
        user,
        source_kind=source_kind,
        output_kind="html",
        language=safe_language,
    )
    target = (
        f"/problems/{problem}/statement/html?source={source_kind}"
        f"&language={quote(safe_language)}&preview_id={quote(row['id'])}"
    )
    return redirect_response(target, status_code=303)


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
