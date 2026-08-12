#!/usr/bin/env python3
"""Report published problem repositories that are not canonical source trees."""

import argparse
import json
import sqlite3
from pathlib import Path

from app.config import build_config_values
from app.service.problem.preflight import (
    PublishedProblemSource,
    inspect_published_problem_sources,
)
from app.service.problem.runtime_config import problem_config_limits
from app.setting import load_settings


def _arguments() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.db_path)
    parser.add_argument("--bare-root", type=Path, default=settings.bare_root)
    return parser.parse_args()


def _read_database(
    path: Path,
) -> tuple[list[PublishedProblemSource], dict[str, object]]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        problems = [
            PublishedProblemSource(
                slug=str(row["slug"]),
                repo_name=str(row["repo_name"]),
            )
            for row in connection.execute(
                "SELECT slug, repo_name FROM problems ORDER BY slug ASC"
            )
        ]
        overrides = {
            str(row["key"]): json.loads(str(row["value_json"]))
            for row in connection.execute(
                "SELECT key, value_json FROM system_config ORDER BY key ASC"
            )
        }
    return problems, overrides


def main() -> int:
    args = _arguments()
    problems, overrides = _read_database(args.db)
    config_values = build_config_values(overrides)
    config_snapshot = config_values.snapshot()
    rows = inspect_published_problem_sources(
        problems,
        bare_root=args.bare_root,
        problem_limits=problem_config_limits(config_values),
        tests_spec_max_bytes=int(config_snapshot["TEXTAREA_MAX_BYTES"]),
        statement_sample_max_bytes=int(
            config_snapshot["STATEMENT_SAMPLE_MAX_BYTES"]
        ),
    )
    failures = [row for row in rows if row["error"]]
    for row in failures:
        revision = row["source_commit"] or "no-main"
        print(f"{row['slug']} ({revision}): {row['error']}")
    print(
        f"checked {len(rows)} published problem repositories; "
        f"{len(failures)} non-canonical"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
