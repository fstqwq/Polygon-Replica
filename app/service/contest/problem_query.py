"""Contest problem read model assembled outside the HTTP layer."""

from pathlib import Path
from typing import Literal, TypedDict

from app.config import ConfigValues
from app.service.access.query import AccessQuery
from app.service.contest.service import ContestProblem, ContestService
from app.service.access.model import ProblemAccessContext
from app.service.platform.fs.layout import StorageLayout
from app.service.problem.content_review import (
    ProblemContentReview,
    problem_content_review,
)
from app.service.problem.context import ProblemMetadataContext, metadata_context
from app.service.problem.authoring_source import inspect_authoring_source
from app.service.problem.build_config import BuildConfig
from app.service.problem.readiness import (
    ProblemReadiness,
    ProblemReadinessService,
    WorkspaceReadinessSubject,
)
from app.service.problem.runtime_config import (
    default_problem_config,
    problem_config_limits,
)
from app.service.problem.source_file import require_regular_source_file
from app.service.problem.source_tree import solution_sources
from app.service.repository.workspace import WorkspaceService
from app.service.statement.context import statement_languages
from app.service.problem.standard_checker import detect_standard_checker
from app.service.workspace.state import WorkspaceState


class ContestProblemDisplayRow(TypedDict):
    contest_problem_id: int
    idx: str
    problem_id: int
    statement_folder: str
    problem_slug: str
    slug_owner: str
    slug_leaf: str
    time_limit_ms: int
    memory_limit_mb: int
    metadata: ProblemMetadataContext
    details_available: bool
    content_review: ProblemContentReview | None
    workspace_revision_local: int | None
    workspace_revision_upstream: int | None
    workspace_revision_available: bool
    workspace_revision_warn: bool
    dirty: bool
    readiness: ProblemReadiness | None
    can_problem_read: bool
    can_problem_write: bool
    created_at: str


def _component_display(
    root: Path,
    build: BuildConfig,
    key: Literal["checker_source", "interactor_source", "validator_source"],
    default_source: str,
) -> tuple[str, bool]:
    source = build.get(key, default_source)
    try:
        path = require_regular_source_file(root, source)
    except ValueError:
        return "missing", False
    if key == "checker_source":
        detected = detect_standard_checker(path)
        if detected:
            return f"std::{detected}", True
    return Path(source).name, True


class ContestProblemQueryService:
    """Assemble ordered contest rows after one batched authorization query."""

    def __init__(
        self,
        contest_service: ContestService,
        access_query: AccessQuery,
        workspace_service: WorkspaceService,
        readiness_service: ProblemReadinessService,
        storage_layout: StorageLayout,
        config_values: ConfigValues,
    ) -> None:
        self._contest_service = contest_service
        self._access_query = access_query
        self._workspace_service = workspace_service
        self._readiness_service = readiness_service
        self._storage_layout = storage_layout
        self._config_values = config_values

    def _resolved_workspace(
        self,
        state: WorkspaceState,
        *,
        problem_slug: str,
        username: str,
    ) -> Path | None:
        try:
            expected = self._storage_layout.workspace(username, problem_slug)
            workspace = Path(state["path"]).resolve()
        except OSError:
            return None
        if workspace != expected:
            return None
        if not workspace.is_dir() or not (workspace / ".git").is_dir():
            return None
        return workspace

    def _workspace_states(
        self,
        rows: list[ContestProblem],
        access_by_problem: dict[int, ProblemAccessContext],
        username: str,
        user_id: int,
    ) -> tuple[dict[int, WorkspaceState], set[int]]:
        readable = [
            row
            for row in rows
            if access_by_problem[row["problem_id"]]["can_read"]
        ]
        problem_ids = [row["problem_id"] for row in readable]
        states = (
            self._workspace_service.workspace_rows(problem_ids, user_id)
            if problem_ids
            else {}
        )
        errors: set[int] = set()
        provisioned = False
        for row in readable:
            problem_id = row["problem_id"]
            state = states.get(problem_id)
            if state is not None and self._resolved_workspace(
                state,
                problem_slug=row["problem_slug"],
                username=username,
            ) is not None:
                continue
            try:
                self._workspace_service.ensure_workspace(
                    row["problem_slug"], username, refresh_status=True
                )
                provisioned = True
            except (OSError, RuntimeError, ValueError):
                errors.add(problem_id)
        if provisioned:
            states = self._workspace_service.workspace_rows(problem_ids, user_id)

        refreshed = False
        for row in readable:
            problem_id = row["problem_id"]
            state = states.get(problem_id)
            if (
                problem_id in errors
                or state is None
                or state["revision_local"] is not None
            ):
                continue
            workspace = self._resolved_workspace(
                state,
                problem_slug=row["problem_slug"],
                username=username,
            )
            if workspace is None:
                errors.add(problem_id)
                continue
            try:
                self._workspace_service.refresh_workspace_status_with_ids(
                    workspace, problem_id, user_id
                )
                refreshed = True
            except (OSError, RuntimeError, ValueError):
                errors.add(problem_id)
        if refreshed:
            states = self._workspace_service.workspace_rows(problem_ids, user_id)
        return states, errors

    def _readiness(
        self,
        rows: list[ContestProblem],
        access_by_problem: dict[int, ProblemAccessContext],
        states: dict[int, WorkspaceState],
        errors: set[int],
        username: str,
    ) -> dict[int, ProblemReadiness]:
        subjects: list[WorkspaceReadinessSubject] = []
        for row in rows:
            problem_id = row["problem_id"]
            if not access_by_problem[problem_id]["can_read"] or problem_id in errors:
                continue
            state = states.get(problem_id)
            if state is None:
                continue
            workspace = self._resolved_workspace(
                state,
                problem_slug=row["problem_slug"],
                username=username,
            )
            if workspace is None:
                errors.add(problem_id)
                continue
            subjects.append(
                {
                    "problem_id": problem_id,
                    "workspace_id": state["id"],
                    "workspace_path": workspace,
                    "head_commit": state["head_commit"],
                    "dirty": bool(state["dirty"]),
                    "local_revision": state["revision_local"],
                    "upstream_revision": state["revision_upstream"],
                    "needs_update": bool(
                        state["revision_upstream_higher"]
                        or (state["revision_behind_count"] or 0) > 0
                    ),
                }
            )
        return (
            self._readiness_service.readiness_many(
                subjects, explain_verification=False
            )
            if subjects
            else {}
        )

    def problem_rows(
        self,
        contest_id: int,
        username: str,
        user_id: int,
        *,
        include_review: bool,
    ) -> list[ContestProblemDisplayRow]:
        rows = self._contest_service.contest_problems(contest_id)
        access = self._access_query.problem_contexts(
            [row["problem_id"] for row in rows], user_id
        )
        states, errors = self._workspace_states(rows, access, username, user_id)
        readiness = (
            self._readiness(rows, access, states, errors, username)
            if include_review
            else {}
        )
        return [
            self._problem_row(
                row,
                access[row["problem_id"]],
                states.get(row["problem_id"]),
                row["problem_id"] in errors,
                readiness.get(row["problem_id"]),
                username,
            )
            for row in rows
        ]

    def _problem_row(
        self,
        row: ContestProblem,
        access: ProblemAccessContext,
        state: WorkspaceState | None,
        workspace_error: bool,
        readiness: ProblemReadiness | None,
        username: str,
    ) -> ContestProblemDisplayRow:
        problem_id = row["problem_id"]
        problem_slug = row["problem_slug"]
        slug_owner, _separator, slug_leaf = problem_slug.partition("/")
        can_read = bool(access["can_read"])
        can_write = bool(access["can_write"])
        metadata = metadata_context(
            default_problem_config(
                limits=problem_config_limits(self._config_values),
            )
        )
        time_limit_ms = metadata["time_limit_ms"]
        memory_limit_mb = metadata["memory_limit_mb"]
        revision_local: int | None = None
        revision_upstream: int | None = None
        revision_available = False
        revision_warn = not can_read
        dirty = False
        test_count = 0
        solution_count = 0
        solutions_truncated = False
        languages: list[str] = []
        output_label = "Checker"
        output_display = "missing"
        validator_display = "missing"
        details_available = False
        review: ProblemContentReview | None = None

        try:
            if not can_read or workspace_error or state is None:
                raise RuntimeError("workspace unavailable")
            workspace = self._resolved_workspace(
                state, problem_slug=problem_slug, username=username
            )
            if workspace is None:
                raise RuntimeError("workspace unavailable")
            revision_local = state["revision_local"]
            revision_upstream = state["revision_upstream"]
            revision_available = True
            revision_warn = bool(
                revision_local is None
                or revision_upstream is None
                or revision_upstream > revision_local
            )
            dirty = bool(state["dirty"])
            source_state = inspect_authoring_source(
                workspace,
                problem_limits=problem_config_limits(self._config_values),
                tests_spec_max_bytes=self._config_values.integer(
                    "TEXTAREA_MAX_BYTES"
                ),
                statement_sample_max_bytes=self._config_values.integer(
                    "STATEMENT_SAMPLE_MAX_BYTES"
                ),
                allow_repair=False,
            )
            problem = source_state["problem"]
            build = source_state["build"]
            metadata = metadata_context(problem)
            time_limit_ms = metadata["time_limit_ms"]
            memory_limit_mb = metadata["memory_limit_mb"]
            mode = metadata["mode"]
            test_count = len(source_state["tests"])
            try:
                all_solutions = solution_sources(workspace)
            except ValueError:
                all_solutions = ()
            solution_limit = self._config_values.integer("SOLUTION_LIST_LIMIT")
            solution_count = min(len(all_solutions), solution_limit)
            solutions_truncated = len(all_solutions) > solution_limit
            languages = statement_languages(workspace)
            validator_display, validator_ready = _component_display(
                workspace, build, "validator_source", "validators/validator.cpp"
            )
            component_key: Literal["checker_source", "interactor_source"] = (
                "interactor_source"
                if mode == "interactive"
                else "checker_source"
            )
            component_default = (
                "interactors/interactor.cpp"
                if mode == "interactive"
                else "checkers/checker.cpp"
            )
            output_label = "Interactor" if mode == "interactive" else "Checker"
            output_display, output_ready = _component_display(
                workspace, build, component_key, component_default
            )
            review = problem_content_review(
                time_limit_ms=time_limit_ms,
                memory_limit_mb=memory_limit_mb,
                test_count=test_count,
                tests_valid=source_state["tests_valid"],
                solution_count=solution_count,
                solutions_truncated=solutions_truncated,
                main_solution_ready=bool(
                    build.get("accepted_solution_source")
                ),
                output_component_label=output_label,
                output_component_display=output_display,
                output_component_ready=output_ready,
                validator_display=validator_display,
                validator_ready=validator_ready,
                statement_language_names=languages,
                source_issues=source_state["issues"],
            )
            details_available = True
        except (OSError, RuntimeError, ValueError):
            if can_read:
                revision_warn = True

        return {
            "contest_problem_id": row["contest_problem_id"],
            "idx": row["idx"],
            "problem_id": problem_id,
            "statement_folder": row["statement_folder"],
            "problem_slug": problem_slug,
            "slug_owner": slug_owner,
            "slug_leaf": slug_leaf,
            "time_limit_ms": time_limit_ms,
            "memory_limit_mb": memory_limit_mb,
            "metadata": metadata,
            "details_available": details_available,
            "content_review": review,
            "workspace_revision_local": revision_local,
            "workspace_revision_upstream": revision_upstream,
            "workspace_revision_available": revision_available,
            "workspace_revision_warn": revision_warn,
            "dirty": dirty,
            "readiness": readiness,
            "can_problem_read": can_read,
            "can_problem_write": can_write,
            "created_at": row["created_at"],
        }
