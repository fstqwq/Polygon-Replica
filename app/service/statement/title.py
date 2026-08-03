from __future__ import annotations

from pathlib import Path

from app.service.statement.context import pick_statement_language
from app.service.statement.render import statement_title_for_language


PROBLEM_TITLE_MAX_LEN = 255


def normalize_problem_title(raw: object, *, fallback_title: str) -> str:
    fallback = str(fallback_title).strip()
    title = ("" if raw is None else str(raw).strip()) or fallback
    if not title:
        raise ValueError("problem title is required")
    if "\n" in title or "\r" in title:
        raise ValueError("problem title must be a single line")
    if len(title) > PROBLEM_TITLE_MAX_LEN:
        raise ValueError(
            f"problem title is too long (max {PROBLEM_TITLE_MAX_LEN})"
        )
    return title


def statement_title_from_snapshot(
    snapshot: Path,
    *,
    fallback_title: str,
    language: str | None = None,
) -> str:
    selected_language = language or pick_statement_language(snapshot)
    return normalize_problem_title(
        statement_title_for_language(
            snapshot,
            selected_language,
            fallback_title=fallback_title,
        ),
        fallback_title=fallback_title,
    )
