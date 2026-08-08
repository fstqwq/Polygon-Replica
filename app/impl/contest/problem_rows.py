from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from app.impl.runtime.config import config
from app.impl.workspace.context_component_status import (
    checker_status_context,
    interactor_status_context,
    validator_status_context,
)
from app.impl.workspace.problem_config import read_problem_config
from app.impl.workspace.solution import list_solution_sources
from app.impl.workspace.test_spec import read_tests_spec
from app.service.problem.resource_limits import resource_limit_display
from app.service.problem_package.service import ProblemReadiness
from app.service.repository.revision import workspace_upstream_revision_display
from app.service.statement.context import statement_languages
from app.service.verification.runtime import (
    coerce_int,
    normalize_pass_limit,
    normalize_problem_mode,
)
from app.service.workspace.state import WorkspaceState

_C = config.constants

PackageRevisionStatus = Literal["ready", "buildable", "blocked"]


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
    workspace_revision_display: str
    workspace_revision_warn: bool
    dirty: bool
    package_revision_display: str
    package_revision_status: PackageRevisionStatus
    published_commit: str
    published_revision_number: int | None
    materialized_commit: str
    materialized_revision_number: int | None
    materialization_id: str
    archive_sha256: str
    current_is_materialized: bool
    package_statement_languages: list[str]
    package_missing_reason: str
    can_problem_read: bool
    can_problem_write: bool
    created_at: str


def package_revision_display(
    readiness: ProblemReadiness,
) -> tuple[str, PackageRevisionStatus]:
    published_revision = readiness["published_revision_number"]
    status = readiness["status"]
    if status == "ready":
        return f"Native ready on v{published_revision}", "ready"
    if status == "buildable":
        return f"Native pending for v{published_revision}", "buildable"
    if published_revision is None:
        return "Native blocked", "blocked"
    return f"Native blocked on v{published_revision}", "blocked"


def _inaccessible_package_readiness(problem_id: int) -> ProblemReadiness:
    return {
        "problem_id": problem_id,
        "published_commit": "",
        "published_revision_number": None,
        "materialized_commit": "",
        "materialized_revision_number": None,
        "materialization_id": "",
        "archive_sha256": "",
        "current_is_materialized": False,
        "status": "blocked",
        "statement_languages": [],
        "missing_reason": "problem access required",
    }


def _workspace_revision_display(
    workspace_state: WorkspaceState,
    readiness: ProblemReadiness,
) -> tuple[str, bool]:
    local_revision = workspace_state["revision_local"]
    published_revision = readiness["published_revision_number"]
    upstream_revision = (
        published_revision
        if published_revision is not None
        else workspace_state["revision_upstream"]
    )
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
        expected = (config.settings.workspace_root / username / problem_slug).resolve()
        workspace = Path(workspace_state["path"]).resolve()
    except OSError:
        return None
    if workspace != expected:
        return None
    if not workspace.is_dir() or not (workspace / ".git").is_dir():
        return None
    return workspace


def contest_problem_rows(
    contest_id: int,
    username: str,
    user_id: int,
) -> list[ContestProblemDisplayRow]:
    rows = config.contest_service.contest_problems(contest_id)
    access_by_problem = config.workspace_service.access_contexts(
        [row["problem_id"] for row in rows],
        user_id,
    )
    readable_rows = [
        row
        for row in rows
        if bool(access_by_problem[row["problem_id"]]["can_read"])
    ]
    # The ACL batch is the boundary for all package and workspace I/O below.
    # Mutation paths persist local status; published readiness supplies the live
    # upstream revision, so a list render does not need to rescan every Git tree.
    readable_problem_ids = [row["problem_id"] for row in readable_rows]
    readiness_by_problem = {
        row["problem_id"]: config.problem_package_service.readiness(
            row["problem_id"]
        )
        for row in readable_rows
    }
    workspace_by_problem = (
        config.workspace_service.workspace_rows(readable_problem_ids, user_id)
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
            config.workspace_service.ensure_workspace(
                row["problem_slug"],
                username,
                refresh_status=True,
            )
            provisioned = True
        except (OSError, RuntimeError, ValueError):
            workspace_errors.add(problem_id)
    if provisioned:
        workspace_by_problem = config.workspace_service.workspace_rows(
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
            config.workspace_service.refresh_workspace_status_with_ids(
                workspace,
                problem_id,
                user_id,
            )
            refreshed = True
        except (OSError, RuntimeError, ValueError):
            workspace_errors.add(problem_id)
    if refreshed:
        workspace_by_problem = config.workspace_service.workspace_rows(
            readable_problem_ids,
            user_id,
        )

    result: list[ContestProblemDisplayRow] = []
    for row in rows:
        problem_id = row["problem_id"]
        problem_slug = row["problem_slug"]
        slug_owner, _separator, slug_leaf = problem_slug.partition("/")
        problem_access = access_by_problem[problem_id]
        can_problem_read = bool(problem_access["can_read"])
        can_problem_write = bool(problem_access["can_write"])

        if can_problem_read:
            readiness = readiness_by_problem[problem_id]
            package_display, package_status = package_revision_display(readiness)
        else:
            readiness = _inaccessible_package_readiness(problem_id)
            package_display = "Package unavailable"
            package_status = "blocked"

        workspace_revision_display = "unavailable"
        workspace_revision_warn = False
        dirty = False
        time_limit_ms = int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"])
        memory_limit_mb = int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"])
        mode = str(_C.GENERAL_CONFIG_DEFAULTS["mode"])
        pass_limit = int(_C.GENERAL_CONFIG_DEFAULTS["pass_limit"])
        test_count = 0
        solution_count = 0
        solutions_truncated = False
        statement_language_names: list[str] = []
        output_component_label = "Checker"
        output_component_display = "missing"
        validator_display = "missing"
        details_available = False

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
                ) = _workspace_revision_display(workspace_state, readiness)
                dirty = bool(workspace_state["dirty"])

                _payload, general_config, _config_path = read_problem_config(workspace)
                time_limit_ms = coerce_int(
                    general_config.get("time_limit_ms"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
                    _C.GENERAL_TIME_LIMIT_MIN_MS,
                    _C.GENERAL_TIME_LIMIT_MAX_MS,
                )
                memory_limit_mb = coerce_int(
                    general_config.get("memory_limit_mb"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["memory_limit_mb"]),
                    _C.GENERAL_MEMORY_LIMIT_MIN_MB,
                    _C.GENERAL_MEMORY_LIMIT_MAX_MB,
                )
                mode = normalize_problem_mode(
                    general_config.get("mode"),
                    str(_C.GENERAL_CONFIG_DEFAULTS["mode"]),
                )
                pass_limit = normalize_pass_limit(
                    general_config.get("pass_limit"),
                    int(_C.GENERAL_CONFIG_DEFAULTS["pass_limit"]),
                )
                test_count = len(read_tests_spec(workspace)[0])
                solution_sources, solutions_truncated = list_solution_sources(
                    workspace,
                    limit=int(_C.SOLUTION_LIST_LIMIT),
                )
                solution_count = len(solution_sources)
                statement_language_names = statement_languages(workspace)
                validator_display = validator_status_context(workspace)["display"]
                if mode == "interactive":
                    output_component_label = "Interactor"
                    output_component_display = interactor_status_context(workspace)[
                        "display"
                    ]
                else:
                    checker_status = checker_status_context(workspace)
                    output_component_display = (
                        checker_status["standard_checker"]
                        or checker_status["display"]
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
                "workspace_revision_display": workspace_revision_display,
                "workspace_revision_warn": workspace_revision_warn,
                "dirty": dirty,
                "package_revision_display": package_display,
                "package_revision_status": package_status,
                "published_commit": readiness["published_commit"],
                "published_revision_number": readiness[
                    "published_revision_number"
                ],
                "materialized_commit": readiness["materialized_commit"],
                "materialized_revision_number": readiness[
                    "materialized_revision_number"
                ],
                "materialization_id": readiness["materialization_id"],
                "archive_sha256": readiness["archive_sha256"],
                "current_is_materialized": readiness["current_is_materialized"],
                "package_statement_languages": readiness["statement_languages"],
                "package_missing_reason": readiness["missing_reason"],
                "can_problem_read": can_problem_read,
                "can_problem_write": can_problem_write,
                "created_at": row["created_at"],
            }
        )
    return result
