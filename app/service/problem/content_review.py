from typing import Literal, TypedDict

from app.service.problem.authoring_source import AuthoringSourceIssue
from app.service.problem.resource_limits import resource_limit_display


ContentReviewTone = Literal["normal", "warning", "danger"]
ContentReviewCode = Literal[
    "source",
    "tests",
    "solutions",
    "output_component",
    "validator",
    "languages",
    "time_limit",
    "memory_limit",
]


class ProblemContentCheck(TypedDict):
    code: ContentReviewCode
    label: str
    display: str
    tone: ContentReviewTone


class ProblemContentReview(TypedDict):
    time_limit: ProblemContentCheck
    memory_limit: ProblemContentCheck
    tests: ProblemContentCheck
    solutions: ProblemContentCheck
    output_component: ProblemContentCheck
    validator: ProblemContentCheck
    languages: ProblemContentCheck
    warnings: list[ProblemContentCheck]
    tone: ContentReviewTone


def _count_display(count: int, singular: str, *, truncated: bool = False) -> str:
    suffix = "+" if truncated else ""
    plural = "" if count == 1 and not truncated else "s"
    return f"{count}{suffix} {singular}{plural}"


def _check(
    code: ContentReviewCode,
    label: str,
    display: str,
    tone: ContentReviewTone,
) -> ProblemContentCheck:
    return {
        "code": code,
        "label": label,
        "display": display,
        "tone": tone,
    }


def problem_content_review(
    *,
    time_limit_ms: int,
    memory_limit_mb: int,
    test_count: int,
    tests_valid: bool,
    solution_count: int,
    solutions_truncated: bool,
    main_solution_ready: bool,
    output_component_label: str,
    output_component_display: str,
    output_component_ready: bool,
    validator_display: str,
    validator_ready: bool,
    statement_language_names: list[str],
    source_issues: list[AuthoringSourceIssue] | None = None,
) -> ProblemContentReview:
    """Build the shared, read-only content checks used before publishing.

    Callers own workspace access and file inspection. This projection only
    applies display and severity semantics; it performs no I/O and does not
    decide whether publishing is allowed.
    """

    limits = resource_limit_display(time_limit_ms, memory_limit_mb)
    time_limit = _check(
        "time_limit",
        "Time limit",
        limits["time_limit_display"],
        "warning" if limits["time_limit_warn"] else "normal",
    )
    memory_limit = _check(
        "memory_limit",
        "Memory limit",
        limits["memory_limit_display"],
        "warning" if limits["memory_limit_warn"] else "normal",
    )

    safe_test_count = max(0, int(test_count))
    if not tests_valid:
        tests_display = "invalid"
        tests_tone: ContentReviewTone = "danger"
    else:
        tests_display = _count_display(safe_test_count, "test")
        tests_tone = "danger" if safe_test_count == 0 else "normal"
    tests = _check("tests", "Tests", tests_display, tests_tone)

    safe_solution_count = max(0, int(solution_count))
    solutions_display = _count_display(
        safe_solution_count,
        "solution",
        truncated=solutions_truncated,
    )
    if safe_solution_count > 0 and not main_solution_ready:
        solutions_display = f"{solutions_display} \u00b7 no main correct"
    solutions = _check(
        "solutions",
        "Solutions",
        solutions_display,
        "normal" if safe_solution_count > 0 and main_solution_ready else "danger",
    )

    output_component = _check(
        "output_component",
        output_component_label,
        output_component_display,
        "normal" if output_component_ready else "danger",
    )
    validator = _check(
        "validator",
        "Validator",
        validator_display,
        "normal" if validator_ready else "danger",
    )

    language_count = len(statement_language_names)
    if language_count == 0:
        language_display = "none"
    elif language_count <= 2:
        language_display = " \u00b7 ".join(statement_language_names)
    else:
        language_display = f"{language_count} languages"
    languages = _check(
        "languages",
        "Languages",
        language_display,
        "danger" if language_count == 0 else "normal",
    )

    source_checks = [
        _check("source", "Source", issue["message"], issue["tone"])
        for issue in (source_issues or [])
    ]
    checks = [
        *source_checks,
        tests,
        solutions,
        output_component,
        validator,
        languages,
        time_limit,
        memory_limit,
    ]
    warnings = [check for check in checks if check["tone"] != "normal"]
    if any(check["tone"] == "danger" for check in warnings):
        tone: ContentReviewTone = "danger"
    elif warnings:
        tone = "warning"
    else:
        tone = "normal"
    return {
        "time_limit": time_limit,
        "memory_limit": memory_limit,
        "tests": tests,
        "solutions": solutions,
        "output_component": output_component,
        "validator": validator,
        "languages": languages,
        "warnings": warnings,
        "tone": tone,
    }
