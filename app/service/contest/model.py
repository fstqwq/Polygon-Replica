from typing import TypedDict


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
