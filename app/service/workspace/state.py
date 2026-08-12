from typing import TypedDict


class WorkspaceState(TypedDict):
    """Storage-neutral workspace status returned by the repository service."""

    id: int
    problem_id: int
    user_id: int
    path: str
    branch: str
    head_commit: str
    dirty: int
    revision_local: int | None
    revision_upstream: int | None
    revision_missing: int
    revision_highlight: int
    revision_upstream_higher: int
    revision_ahead_count: int | None
    revision_behind_count: int | None
    updated_at: str
