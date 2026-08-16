"""Shared typed presentation facts derived from canonical Problem source."""

from typing import Literal, TypedDict

from app.service.problem.resource_limits import resource_limit_display
from app.service.problem.runtime_config import ProblemConfig
from app.service.problem.test_spec import TestSpecEntry, summarize_tests_spec


ContextTone = Literal["normal", "muted", "warning", "danger"]


class StatusContext(TypedDict):
    state: str
    text: str
    tone: ContextTone
    hint: str


class ProblemMetadataContext(TypedDict):
    time_limit_ms: int
    memory_limit_mb: int
    mode: Literal["pass-fail", "interactive"]
    pass_limit: int
    time_limit_display: str
    time_limit_warn: bool
    memory_limit_display: str
    memory_limit_warn: bool


class ProblemTestsContext(TypedDict):
    mode: Literal["ready", "empty", "invalid"]
    display: str
    total: int
    manual: int
    gen: int
    sample: int


def metadata_context(problem: ProblemConfig) -> ProblemMetadataContext:
    time_limit_ms = problem["time_limit_ms"]
    memory_limit_mb = problem["memory_limit_mb"]
    return {
        "time_limit_ms": time_limit_ms,
        "memory_limit_mb": memory_limit_mb,
        "mode": problem["mode"],
        "pass_limit": problem["pass_limit"],
        **resource_limit_display(time_limit_ms, memory_limit_mb),
    }


def status_context(
    *,
    state: str,
    text: str,
    tone: ContextTone = "normal",
    hint: str = "",
) -> StatusContext:
    return {
        "state": state,
        "text": text,
        "tone": tone,
        "hint": hint,
    }


def tests_status_context(
    entries: list[TestSpecEntry],
    *,
    valid: bool,
) -> ProblemTestsContext:
    """Project one already-loaded test specification for all UI consumers."""

    if not valid:
        return {
            "mode": "invalid",
            "display": "invalid",
            "total": 0,
            "manual": 0,
            "gen": 0,
            "sample": 0,
        }
    summary = summarize_tests_spec(entries)
    total = summary["total"]
    if total == 0:
        return {
            "mode": "empty",
            "display": "empty",
            "total": summary["total"],
            "manual": summary["manual"],
            "gen": summary["gen"],
            "sample": summary["sample"],
        }
    sample = summary["sample"]
    sample_label = "sample" if sample == 1 else "samples"
    return {
        "mode": "ready",
        "display": f"{total} ({sample} {sample_label})",
        "total": summary["total"],
        "manual": summary["manual"],
        "gen": summary["gen"],
        "sample": summary["sample"],
    }
