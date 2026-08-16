"""Read-only validation of canonical sources at published Git revisions."""

import tempfile
from pathlib import Path
from typing import TypedDict

from app.service.platform.fs.op import extract_git_archive
from app.service.platform.git_process import run_git
from app.service.problem.runtime_config import ProblemConfigLimits
from app.service.problem.source_tree import load_problem_source_tree


class PublishedProblemSource(TypedDict):
    slug: str
    repo_name: str


class ProblemSourcePreflightRow(TypedDict):
    slug: str
    source_commit: str
    error: str


def inspect_published_problem_sources(
    problems: list[PublishedProblemSource],
    *,
    bare_root: Path,
    problem_limits: ProblemConfigLimits,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> list[ProblemSourcePreflightRow]:
    root = bare_root.resolve(strict=True)
    rows: list[ProblemSourcePreflightRow] = []
    for problem in problems:
        slug = problem["slug"]
        source_commit = ""
        error = ""
        try:
            unresolved = root / problem["repo_name"]
            if unresolved.is_symlink() or not unresolved.is_dir():
                raise ValueError("bare repository is missing")
            repository = unresolved.resolve(strict=True)
            if root not in repository.parents:
                raise ValueError("bare repository escapes the configured root")
            head = run_git(
                [
                    "git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    "--verify",
                    "refs/heads/main^{commit}",
                ],
                timeout=120,
            )
            if head.returncode != 0:
                continue
            source_commit = head.stdout.strip()
            with tempfile.TemporaryDirectory(
                prefix="problem-source-preflight-"
            ) as temporary:
                snapshot = Path(temporary) / "source"
                extract_git_archive(
                    repository,
                    source_commit,
                    snapshot,
                    timeout=120,
                )
                for derived_root in ("test-data", "statement-build"):
                    derived = snapshot / derived_root
                    if derived.is_symlink() or derived.exists():
                        raise ValueError(
                            f"{derived_root}: derived package payloads must not "
                            "be committed"
                        )
                load_problem_source_tree(
                    snapshot,
                    problem_limits=problem_limits,
                    tests_spec_max_bytes=tests_spec_max_bytes,
                    statement_sample_max_bytes=statement_sample_max_bytes,
                )
        except (OSError, RuntimeError, ValueError) as exc:
            error = str(exc)
        rows.append(
            {
                "slug": slug,
                "source_commit": source_commit,
                "error": error,
            }
        )
    return rows
