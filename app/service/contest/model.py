from __future__ import annotations

from typing import Literal, TypedDict


class ContestBuildFreezeResult(TypedDict):
    outcome: Literal[
        "created",
        "already_running",
        "busy",
        "roster_changed",
        "not_ready",
    ]
    job_id: str
    contest_slug: str
    blocked_problems: list[str]


class ContestBuildRevision(TypedDict):
    contest_problem_id: int
    position: int
    label: str
    problem_id: int
    statement_folder: str
    problem_slug: str
    source_commit: str
    revision_number: int
