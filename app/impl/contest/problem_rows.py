import app.main_constant as _K

from pathlib import Path
from typing import TypedDict

from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_component_status import (
    checker_status_context,
    interactor_status_context,
    validator_status_context,
)
from app.impl.workspace.solution import list_solution_sources
from app.service.problem.content_review import (
    ProblemContentReview,
    problem_content_review,
)
from app.service.problem.resource_limits import resource_limit_display
from app.service.problem.runtime_config import problem_config_limits
from app.service.problem.readiness import ProblemReadiness, WorkspaceReadinessSubject
from app.service.problem.source_tree import load_problem_source_tree
from app.service.repository.revision import workspace_upstream_revision_display
from app.service.statement.context import statement_languages
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
    time_limit_display: str
    time_limit_warn: bool
    memory_limit_display: str
    memory_limit_warn: bool
    mode: str
    pass_limit: int
    test_count: int
    solution_count: int
    solutions_truncated: bool
    statement_language_names: list[str]
    statement_language_count: int
    output_component_label: str
    output_component_display: str
    validator_display: str
    details_available: bool
    content_review: ProblemContentReview | None
    workspace_revision_display: str
    workspace_revision_warn: bool
    dirty: bool
    readiness: ProblemReadiness | None
    can_problem_read: bool
    can_problem_write: bool
    created_at: str


def _workspace_revision_display(
    workspace_state: WorkspaceState,
) -> tuple[str, bool]:
    local_revision = workspace_state["revision_local"]
    upstream_revision = workspace_state["revision_upstream"]
    warn = (
        local_revision is None
        or upstream_revision is None
        or upstream_revision > local_revision
    )
    return (
        workspace_upstream_revision_display(local_revision, upstream_revision),
        warn,
    )


def _resolved_workspace_path(
    workspace_state: WorkspaceState,
    *,
    problem_slug: str,
    username: str,
) -> Path | None:
    try:
        expected = runtime().storage_layout.workspace(username, problem_slug)
        workspace = Path(workspace_state["path"]).resolve()
    except OSError:
        return None
    if workspace != expected:
        return None
    if not workspace.is_dir() or not (workspace / ".git").is_dir():
        return None
    return workspace


def _contest_problem_rows(
    contest_id: int,
    username: str,
    user_id: int,
    *,
    include_review: bool,
) -> list[ContestProblemDisplayRow]:
    config_snapshot = runtime().config_values.snapshot()
    rows = runtime().contest_service.contest_problems(contest_id)
    access_by_problem = runtime().access_query.problem_contexts(
        [row["problem_id"] for row in rows],
        user_id,
    )
    readable_rows = [
        row
        for row in rows
        if bool(access_by_problem[row["problem_id"]]["can_read"])
    ]
    # The ACL batch is the boundary for all package and workspace I/O below.
    # Mutation paths persist local status. A list render only refreshes a
    # workspace whose revision state has never been recorded.
    readable_problem_ids = [row["problem_id"] for row in readable_rows]
    workspace_by_problem = (
        runtime().workspace_service.workspace_rows(readable_problem_ids, user_id)
        if readable_problem_ids
        else {}
    )
    workspace_errors: set[int] = set()
    provisioned = False
    for row in readable_rows:
        problem_id = row["problem_id"]
        workspace_state = workspace_by_problem.get(problem_id)
        workspace = (
            _resolved_workspace_path(
                workspace_state,
                problem_slug=row["problem_slug"],
                username=username,
            )
            if workspace_state is not None
            else None
        )
        if workspace is not None:
            continue
        try:
            runtime().workspace_service.ensure_workspace(
                row["problem_slug"],
                username,
                refresh_status=True,
            )
            provisioned = True
        except (OSError, RuntimeError, ValueError):
            workspace_errors.add(problem_id)
    if provisioned:
        workspace_by_problem = runtime().workspace_service.workspace_rows(
            readable_problem_ids,
            user_id,
        )

    refreshed = False
    for row in readable_rows:
        problem_id = row["problem_id"]
        if problem_id in workspace_errors:
            continue
        workspace_state = workspace_by_problem.get(problem_id)
        if workspace_state is None or workspace_state["revision_local"] is not None:
            continue
        workspace = _resolved_workspace_path(
            workspace_state,
            problem_slug=row["problem_slug"],
            username=username,
        )
        if workspace is None:
            workspace_errors.add(problem_id)
            continue
        try:
            runtime().workspace_service.refresh_workspace_status_with_ids(
                workspace,
                problem_id,
                user_id,
            )
            refreshed = True
        except (OSError, RuntimeError, ValueError):
            workspace_errors.add(problem_id)
    if refreshed:
        workspace_by_problem = runtime().workspace_service.workspace_rows(
            readable_problem_ids,
            user_id,
        )

    readiness_subjects: list[WorkspaceReadinessSubject] = []
    if include_review:
        for row in readable_rows:
            problem_id = row["problem_id"]
            if problem_id in workspace_errors:
                continue
            workspace_state = workspace_by_problem.get(problem_id)
            if workspace_state is None:
                continue
            workspace = _resolved_workspace_path(
                workspace_state,
                problem_slug=row["problem_slug"],
                username=username,
            )
            if workspace is None:
                workspace_errors.add(problem_id)
                continue
            behind_count = workspace_state["revision_behind_count"] or 0
            readiness_subjects.append(
                {
                    "problem_id": problem_id,
                    "workspace_id": workspace_state["id"],
                    "workspace_path": workspace,
                    "head_commit": workspace_state["head_commit"],
                    "dirty": bool(workspace_state["dirty"]),
                    "local_revision": workspace_state["revision_local"],
                    "upstream_revision": workspace_state["revision_upstream"],
                    "needs_update": bool(
                        workspace_state["revision_upstream_higher"]
                        or behind_count > 0
                    ),
                }
            )
    readiness_by_problem = (
        runtime().problem_readiness_service.readiness_many(
            readiness_subjects,
            explain_verification=False,
        )
        if readiness_subjects
        else {}
    )

    result: list[ContestProblemDisplayRow] = []
    for row in rows:
        problem_id = row["problem_id"]
        problem_slug = row["problem_slug"]
        slug_owner, _separator, slug_leaf = problem_slug.partition("/")
        problem_access = access_by_problem[problem_id]
        can_problem_read = bool(problem_access["can_read"])
        can_problem_write = bool(problem_access["can_write"])

        readiness = readiness_by_problem.get(problem_id)

        workspace_revision_display = "unavailable"
        workspace_revision_warn = False
        dirty = False
        time_limit_ms = int(_K.GENERAL_CONFIG_DEFAULTS["time_limit_ms"])
        memory_limit_mb = int(_K.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"])
        mode = str(_K.GENERAL_CONFIG_DEFAULTS["mode"])
        pass_limit = int(_K.GENERAL_CONFIG_DEFAULTS["pass_limit"])
        test_count = 0
        solution_count = 0
        solutions_truncated = False
        statement_language_names: list[str] = []
        output_component_label = "Checker"
        output_component_display = "missing"
        validator_display = "missing"
        details_available = False
        content_review: ProblemContentReview | None = None

        if can_problem_read:
            try:
                if problem_id in workspace_errors:
                    raise RuntimeError("workspace unavailable")
                workspace_state = workspace_by_problem[problem_id]
                workspace = _resolved_workspace_path(
                    workspace_state,
                    problem_slug=problem_slug,
                    username=username,
                )
                if workspace is None:
                    raise RuntimeError("workspace unavailable")
                (
                    workspace_revision_display,
                    workspace_revision_warn,
                ) = _workspace_revision_display(workspace_state)
                dirty = bool(workspace_state["dirty"])

                source_tree = load_problem_source_tree(
                    workspace,
                    problem_limits=problem_config_limits(runtime().config_values),
                    tests_spec_max_bytes=int(
                        config_snapshot["TEXTAREA_MAX_BYTES"]
                    ),
                    statement_sample_max_bytes=int(
                        config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
                    ),
                )
                time_limit_ms = source_tree.problem["time_limit_ms"]
                memory_limit_mb = source_tree.problem["memory_limit_mb"]
                mode = source_tree.problem["mode"]
                pass_limit = source_tree.problem["pass_limit"]
                test_count = len(source_tree.tests)
                solution_sources, solutions_truncated = list_solution_sources(
                    workspace,
                    limit=int(runtime().config_values.SOLUTION_LIST_LIMIT),
                )
                solution_count = len(solution_sources)
                statement_language_names = statement_languages(workspace)
                validator_status = validator_status_context(workspace)
                validator_display = validator_status["display"]
                if mode == "interactive":
                    output_component_label = "Interactor"
                    output_component_status = interactor_status_context(workspace)
                    output_component_display = output_component_status["display"]
                else:
                    checker_status = checker_status_context(workspace)
                    output_component_status = checker_status
                    output_component_display = (
                        checker_status["standard_checker"]
                        or checker_status["display"]
                    )
                content_review = problem_content_review(
                    time_limit_ms=time_limit_ms,
                    memory_limit_mb=memory_limit_mb,
                    test_count=test_count,
                    tests_valid=True,
                    solution_count=solution_count,
                    solutions_truncated=solutions_truncated,
                    main_solution_ready=bool(
                        source_tree.build.get("accepted_solution_source")
                    ),
                    output_component_label=output_component_label,
                    output_component_display=output_component_display,
                    output_component_ready=(
                        output_component_status["mode"] == "repository"
                    ),
                    validator_display=validator_display,
                    validator_ready=validator_status["mode"] == "repository",
                    statement_language_names=statement_language_names,
                )
                details_available = True
            except (OSError, RuntimeError, ValueError):
                workspace_revision_display = "unavailable"
                workspace_revision_warn = True
        else:
            workspace_revision_display = "no problem access"
            workspace_revision_warn = True

        result.append(
            {
                "contest_problem_id": row["contest_problem_id"],
                "idx": row["idx"],
                "problem_id": problem_id,
                "statement_folder": row["statement_folder"],
                "problem_slug": problem_slug,
                "slug_owner": slug_owner,
                "slug_leaf": slug_leaf,
                "time_limit_ms": time_limit_ms,
                "memory_limit_mb": memory_limit_mb,
                **resource_limit_display(time_limit_ms, memory_limit_mb),
                "mode": mode,
                "pass_limit": pass_limit,
                "test_count": test_count,
                "solution_count": solution_count,
                "solutions_truncated": solutions_truncated,
                "statement_language_names": statement_language_names,
                "statement_language_count": len(statement_language_names),
                "output_component_label": output_component_label,
                "output_component_display": output_component_display,
                "validator_display": validator_display,
                "details_available": details_available,
                "content_review": content_review,
                "workspace_revision_display": workspace_revision_display,
                "workspace_revision_warn": workspace_revision_warn,
                "dirty": dirty,
                "readiness": readiness,
                "can_problem_read": can_problem_read,
                "can_problem_write": can_problem_write,
                "created_at": row["created_at"],
            }
        )
    return result


def contest_overview_problem_rows(
    contest_id: int,
    username: str,
    user_id: int,
) -> list[ContestProblemDisplayRow]:
    return _contest_problem_rows(
        contest_id,
        username,
        user_id,
        include_review=True,
    )


def contest_management_problem_rows(
    contest_id: int,
    username: str,
    user_id: int,
) -> list[ContestProblemDisplayRow]:
    return _contest_problem_rows(
        contest_id,
        username,
        user_id,
        include_review=False,
    )
