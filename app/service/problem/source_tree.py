"""Load and validate the canonical authored problem source tree."""

from dataclasses import dataclass
from pathlib import Path

from app.main_constant import SOLUTION_SOURCE_EXTENSIONS
from app.service.problem.build_config import BuildConfig, load_build_config
from app.service.problem.runtime_config import (
    ProblemConfig,
    ProblemConfigLimits,
    load_problem_config,
)
from app.service.problem.solution_metadata import (
    ExpectedBehavior,
    load_solution_desc,
)
from app.service.problem.source_file import (
    require_regular_source_file,
    validate_source_tree_filesystem,
)
from app.service.problem.test_spec import (
    TestSpecEntry,
    generator_source_paths,
    load_tests_spec,
    parse_gen_command_tokens,
    payload_rel_path_for_test,
    resolve_generator_source,
)


@dataclass(frozen=True)
class ProblemSourceTree:
    problem: ProblemConfig
    build: BuildConfig
    tests: tuple[TestSpecEntry, ...]
    solution_behaviors: dict[str, ExpectedBehavior]


def solution_sources(root: Path) -> tuple[str, ...]:
    directory = root / "solutions"
    if directory.is_symlink():
        raise ValueError("solutions: must not be a symbolic link")
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError("solutions: must be a directory")
    sources: list[str] = []
    try:
        for path in directory.iterdir():
            if path.suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
                continue
            relative = path.relative_to(root).as_posix()
            require_regular_source_file(root, relative)
            sources.append(relative)
    except OSError as exc:
        raise ValueError(f"solutions: cannot list directory: {exc}") from exc
    return tuple(sorted(sources))


def load_problem_source_tree(
    root: Path,
    *,
    problem_limits: ProblemConfigLimits,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> ProblemSourceTree:
    validate_source_tree_filesystem(root)
    problem = load_problem_config(root, limits=problem_limits)
    build = load_build_config(root)
    tests = tuple(
        load_tests_spec(
            root / "tests/spec.json",
            document_max_bytes=tests_spec_max_bytes,
            sample_max_bytes=statement_sample_max_bytes,
        )
    )
    generator_sources = tuple(generator_source_paths(root))

    selected_sources = [
        build[key]
        for key in (
            "accepted_solution_source",
            "validator_source",
            "checker_source",
            "interactor_source",
        )
        if key in build
    ]
    for relative in selected_sources:
        require_regular_source_file(root, relative)

    for entry in tests:
        relative = payload_rel_path_for_test(entry["id"], entry["kind"])
        payload_path = require_regular_source_file(root, relative)
        try:
            payload = payload_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{relative}: must be UTF-8") from exc
        except OSError as exc:
            raise ValueError(f"{relative}: cannot read file: {exc}") from exc
        if entry["kind"] == "gen":
            tokens = parse_gen_command_tokens(payload)
            resolve_generator_source(tokens[0], generator_sources)

    behaviors: dict[str, ExpectedBehavior] = {}
    for relative in solution_sources(root):
        behaviors[relative] = load_solution_desc(root, relative)[
            "expected_behavior"
        ]

    accepted_source = build.get("accepted_solution_source")
    if accepted_source is not None:
        behaviors[accepted_source] = "accepted"

    if problem["mode"] == "interactive" and "checker_source" in build:
        raise ValueError(
            "config/build.json.checker_source: not used in interactive mode"
        )
    if problem["mode"] == "pass-fail" and "interactor_source" in build:
        raise ValueError(
            "config/build.json.interactor_source: not used in pass-fail mode"
        )

    return ProblemSourceTree(
        problem=problem,
        build=build,
        tests=tests,
        solution_behaviors=behaviors,
    )
