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
    ordinal: int
    idx: str
    problem_id: int
    problem_slug: str
    statement_folder: str
    source_commit: str
    revision_number: int
    materialization_id: str
    archive_sha256: str


class AgentContestRosterProblem(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    problem_slug: str


class AgentContestRoster(TypedDict):
    contest_id: int
    contest_slug: str
    contest_title: str
    source_generation: int
    problems: list[AgentContestRosterProblem]
