"""Public state contract for disposable statement previews."""

from typing import Literal, NotRequired, Protocol, TypedDict


StatementPreviewSubject = Literal["problem", "contest"]
StatementPreviewSource = Literal["workspace", "native_package"]
StatementPreviewOutput = Literal["html", "pdf"]


class ContestStatementPreviewItem(TypedDict):
    idx: str
    problem_id: int
    problem_slug: str
    preview_id: str
    status: str
    error: str


class StatementPdfProblemResult(TypedDict):
    idx: str
    problem_id: int
    problem_slug: str
    source_folder: str
    status: str
    error: str
    preamble_lines: NotRequired[list[str]]


class StatementPdfTotals(TypedDict):
    total: int
    success: int
    failed: int


class StatementPreviewSummary(TypedDict, total=False):
    error: str
    content: str
    warnings: list[str]
    resources: list[str]
    sample_count: int
    returncode: int | None
    pdf: str
    items: list[ContestStatementPreviewItem]
    successful: int
    failed: int
    job_type: str
    contest_slug: str
    language: str
    results: list[StatementPdfProblemResult]
    totals: StatementPdfTotals
    latex_log: str
    filename: str
    log: str


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
    status: str
    summary: StatementPreviewSummary
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
    ) -> None: ...

    def finish(
        self,
        preview_id: str,
        *,
        status: str,
        summary: StatementPreviewSummary,
    ) -> None: ...

    def row(
        self,
        preview_id: str,
        *,
        actor_user_id: int | None = None,
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
    ) -> StatementPreviewRow | None: ...
