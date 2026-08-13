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
