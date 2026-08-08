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
from app.service.repository.revision import workspace_revision_info
from app.service.statement.context import statement_languages
from app.service.verification.runtime import (
    coerce_int,
    normalize_pass_limit,
    normalize_problem_mode,
)

_C = config.constants

PackageRevisionStatus = Literal["current", "stale", "missing"]


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
    materialized_revision = readiness["materialized_revision_number"]
    if materialized_revision is None:
        return "Package none", "missing"
    status: PackageRevisionStatus = (
        "current" if readiness["current_is_materialized"] else "stale"
    )
    return f"Package on v{materialized_revision}", status


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
        "statement_languages": [],
        "missing_reason": "problem access required",
    }


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
    result: list[ContestProblemDisplayRow] = []
    for row in rows:
        problem_id = row["problem_id"]
        problem_slug = row["problem_slug"]
        slug_owner, _separator, slug_leaf = problem_slug.partition("/")
        problem_access = access_by_problem[problem_id]
        can_problem_read = bool(problem_access["can_read"])
        can_problem_write = bool(problem_access["can_write"])

        if can_problem_read:
            readiness = config.problem_package_service.readiness(problem_id)
            package_display, package_status = package_revision_display(readiness)
        else:
            readiness = _inaccessible_package_readiness(problem_id)
            package_display = "Package unavailable"
            package_status = "missing"

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
                workspace = Path(
                    config.workspace_service.ensure_workspace(
                        problem_slug,
                        username,
                        refresh_status=True,
                    )
                )
                workspace_context = config.workspace_service.workspace_context(
                    problem_slug,
                    username,
                    include_recent=False,
                )
                workspace_state = workspace_context["workspace"]
                branch = str(workspace_state.get("branch") or "main")
                revision = workspace_revision_info(workspace, branch)
                workspace_revision_display = revision["display"]
                workspace_revision_warn = revision["highlight"]
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
