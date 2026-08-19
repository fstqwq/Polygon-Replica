"""Fault-tolerant source inspection for problem authoring pages."""

from pathlib import Path
from typing import Literal, TypedDict

from app.service.problem.build_config import (
    BUILD_CONFIG_REL,
    AuthoringBuildConfig,
    BuildConfig,
    dumps_build_config,
    inspect_authoring_build_config,
    mode_extra_build_field_message,
)
from app.service.problem.runtime_config import (
    ProblemConfig,
    ProblemConfigLimits,
    ProblemMode,
    default_problem_config,
    load_problem_config,
)
from app.service.problem.source_file import require_regular_source_file
from app.service.problem.source_tree import validate_problem_source_tree
from app.service.problem.test_spec import (
    TESTS_SPEC_REL,
    TestSpecEntry,
    load_tests_spec,
)


AuthoringSourceIssueTone = Literal["warning", "danger"]


class AuthoringSourceIssue(TypedDict):
    message: str
    tone: AuthoringSourceIssueTone


class AuthoringSourceState(TypedDict):
    problem: ProblemConfig
    build: BuildConfig
    tests: list[TestSpecEntry]
    tests_valid: bool
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


def _build_result(
    root: Path,
    *,
    problem_mode: ProblemMode | None,
) -> AuthoringBuildConfig:
    try:
        text = _read_utf8(root, BUILD_CONFIG_REL.as_posix())
    except ValueError as exc:
        return {
            "config": BuildConfig(generator_sources=[]),
            "removed_keys": (),
            "extra_fields": (),
            "error": str(exc),
        }
    return inspect_authoring_build_config(text, problem_mode=problem_mode)


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
    Export, Contest package downloads, and package materialization.
    """

    issues: list[AuthoringSourceIssue] = []
    problem_valid = True
    try:
        problem = load_problem_config(root, limits=problem_limits)
    except ValueError as exc:
        problem_valid = False
        problem = default_problem_config(limits=problem_limits)
        _append_issue(issues, str(exc), "danger")

    problem_mode = problem["mode"] if problem_valid else None
    build_result = _build_result(root, problem_mode=problem_mode)
    build = build_result["config"]
    build_valid = not build_result["error"]
    build_normalized = False
    if build_result["error"]:
        _append_issue(issues, build_result["error"], "danger")
    else:
        for field in build_result["extra_fields"]:
            assert problem_mode is not None
            _append_issue(
                issues,
                mode_extra_build_field_message(
                    field,
                    problem_mode=problem_mode,
                    label="config/build.json",
                ),
                "warning",
            )

    if not build_result["error"] and build_result["removed_keys"]:
        message = _normalization_message(build_result["removed_keys"])
        if allow_repair and not build_result["extra_fields"]:
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
            fields = ", ".join(build_result["removed_keys"])
            _append_issue(
                issues,
                "config/build.json: contains obsolete fields "
                f"({fields}); review and publish a normalized configuration",
                "warning",
            )

    if published_build_text is not None:
        published = inspect_authoring_build_config(
            published_build_text,
            problem_mode=problem_mode,
        )
        if published["removed_keys"] and not build_result["removed_keys"]:
            _append_issue(
                issues,
                _normalization_message(published["removed_keys"]),
                "warning",
            )

    tests: list[TestSpecEntry] = []
    tests_valid = True
    try:
        tests = load_tests_spec(
            root / TESTS_SPEC_REL,
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
    except ValueError as exc:
        tests_valid = False
        _append_issue(issues, str(exc), "danger")

    if (
        problem_valid
        and build_valid
        and tests_valid
    ):
        try:
            validate_problem_source_tree(
                root,
                problem=problem,
                build=build,
                tests=tuple(tests),
            )
        except ValueError as exc:
            _append_issue(issues, str(exc), "danger")

    return {
        "problem": problem,
        "build": build,
        "tests": tests,
        "tests_valid": tests_valid,
        "issues": issues,
        "build_normalized": build_normalized,
    }
