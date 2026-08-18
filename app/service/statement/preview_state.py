"""Public state contract for disposable statement previews."""

from typing import Literal, Protocol, TypedDict


StatementPreviewSubject = Literal["problem", "contest"]
StatementPreviewSource = Literal["workspace", "native_package"]
StatementPreviewOutput = Literal["html", "pdf"]


class StatementPreviewRow(TypedDict):
    id: str
    actor_user_id: int
    subject_kind: StatementPreviewSubject
    problem_id: int | None
    contest_id: int | None
    source_kind: StatementPreviewSource
    output_kind: StatementPreviewOutput
    language: str
    input_identity: str
    options: dict[str, object]
    status: str
    summary: dict[str, object]
    created_at: str
    finished_at: str


class StatementPreviewRepository(Protocol):
    """Persistence operations required by statement preview services."""

    def insert(
        self,
        *,
        preview_id: str,
        actor_user_id: int,
        subject_kind: StatementPreviewSubject,
        problem_id: int | None,
        contest_id: int | None,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
        options: dict[str, object],
    ) -> None: ...

    def finish(
        self,
        preview_id: str,
        *,
        status: str,
        summary: dict[str, object],
    ) -> None: ...

    def row(
        self,
        preview_id: str,
        *,
        actor_user_id: int | None = None,
    ) -> StatementPreviewRow | None: ...

    def latest_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
    ) -> StatementPreviewRow | None: ...

    def latest_contest(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
    ) -> StatementPreviewRow | None: ...

    def cached_problem(
        self,
        problem_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
    ) -> StatementPreviewRow | None: ...

    def cached_contest(
        self,
        contest_id: int,
        *,
        actor_user_id: int,
        source_kind: StatementPreviewSource,
        output_kind: StatementPreviewOutput,
        language: str,
        input_identity: str,
        options: dict[str, object],
    ) -> StatementPreviewRow | None: ...
