from typing import Literal, TypedDict


class ContestBuildFreezeResult(TypedDict):
    outcome: Literal[
        "created",
        "already_running",
        "busy",
        "not_ready",
    ]
    job_id: str
    contest_slug: str
    blocked_problems: list[str]


class ContestBuildItemRecord(TypedDict):
    contest_problem_id: int
    position: int
    label: str
    idx: str
    problem_id: int
    problem_slug: str
    statement_folder: str
    source_commit: str
    revision_number: int
    materialization_id: str
    archive_sha256: str
