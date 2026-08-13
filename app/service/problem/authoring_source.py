"""Fault-tolerant source inspection for problem authoring pages."""

from pathlib import Path
from typing import Literal, TypedDict

from app.service.problem.build_config import (
    BUILD_CONFIG_REL,
    AuthoringBuildConfig,
    BuildConfig,
    dumps_build_config,
    inspect_authoring_build_config,
)
from app.service.problem.runtime_config import (
    ProblemConfig,
    ProblemConfigLimits,
    default_problem_config,
    load_problem_config,
)
from app.service.problem.source_file import require_regular_source_file
from app.service.problem.source_tree import load_problem_source_tree
from app.service.problem.test_spec import TESTS_SPEC_REL, load_tests_spec


AuthoringSourceIssueTone = Literal["warning", "danger"]


class AuthoringSourceIssue(TypedDict):
    message: str
    tone: AuthoringSourceIssueTone


class AuthoringSourceState(TypedDict):
    problem: ProblemConfig
    build: BuildConfig
    issues: list[AuthoringSourceIssue]
    build_normalized: bool


def _read_utf8(root: Path, relative: str) -> str:
    path = require_regular_source_file(root, relative)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{relative}: must be UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"{relative}: cannot read file: {exc}") from exc


def _append_issue(
    issues: list[AuthoringSourceIssue],
    message: str,
    tone: AuthoringSourceIssueTone,
) -> None:
    if any(issue["message"] == message for issue in issues):
        return
    issues.append({"message": message, "tone": tone})


def _build_result(root: Path) -> AuthoringBuildConfig:
    try:
        text = _read_utf8(root, BUILD_CONFIG_REL.as_posix())
    except ValueError as exc:
        return {"config": BuildConfig(), "removed_keys": (), "error": str(exc)}
    return inspect_authoring_build_config(text)


def _normalization_message(keys: tuple[str, ...]) -> str:
    fields = ", ".join(keys)
    return (
        "config/build.json: obsolete fields were removed "
        f"({fields}); review and publish the normalized configuration"
    )


def inspect_authoring_source(
    root: Path,
    *,
    problem_limits: ProblemConfigLimits,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
    allow_repair: bool,
    published_build_text: str | None = None,
) -> AuthoringSourceState:
    """Return complete page inputs while preserving strict consumer checks.

    Invalid authoring files become diagnostics and page-local defaults. Known
    obsolete build fields are safely removed for writable workspaces. The
    strict source-tree loader remains the final authority for Verification,
    Export, Contest builds, and package materialization.
    """

    issues: list[AuthoringSourceIssue] = []
    problem_valid = True
    try:
        problem = load_problem_config(root, limits=problem_limits)
    except ValueError as exc:
        problem_valid = False
        problem = default_problem_config(limits=problem_limits)
        _append_issue(issues, str(exc), "danger")

    build_result = _build_result(root)
    build = build_result["config"]
    build_valid = not build_result["error"]
    build_normalized = False
    if build_result["error"]:
        _append_issue(issues, build_result["error"], "danger")
    elif build_result["removed_keys"]:
        message = _normalization_message(build_result["removed_keys"])
        if allow_repair:
            path = root / BUILD_CONFIG_REL
            try:
                path.write_text(
                    dumps_build_config(build),
                    encoding="utf-8",
                    newline="\n",
                )
                build_normalized = True
                _append_issue(issues, message, "warning")
            except OSError as exc:
                build_valid = False
                _append_issue(
                    issues,
                    f"config/build.json: cannot write normalized configuration: {exc}",
                    "danger",
                )
        else:
            build_valid = False
            _append_issue(
                issues,
                "config/build.json: contains obsolete fields; an author must "
                "review and publish a normalized configuration",
                "danger",
            )

    if published_build_text is not None:
        published = inspect_authoring_build_config(published_build_text)
        if published["removed_keys"] and not build_result["removed_keys"]:
            _append_issue(
                issues,
                _normalization_message(published["removed_keys"]),
                "warning",
            )

    tests_valid = True
    try:
        load_tests_spec(
            root / TESTS_SPEC_REL,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
    except ValueError as exc:
        tests_valid = False
        _append_issue(issues, str(exc), "danger")

    if problem_valid and build_valid and tests_valid:
        try:
            load_problem_source_tree(
                root,
                problem_limits=problem_limits,
                tests_spec_max_bytes=tests_spec_max_bytes,
                statement_sample_max_bytes=statement_sample_max_bytes,
            )
        except ValueError as exc:
            _append_issue(issues, str(exc), "danger")

    return {
        "problem": problem,
        "build": build,
        "issues": issues,
        "build_normalized": build_normalized,
    }
