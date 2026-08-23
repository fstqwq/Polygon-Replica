import json
import tempfile
import unittest
from pathlib import Path

from app.config import build_config_values
from app.service.importing.contest import PolygonContestImportService
from app.service.importing.archive import (
    ArchiveView,
    contest_archive_policy,
    problem_archive_policy,
)
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.polygon import PolygonPackageImportService
from app.service.problem.runtime_config import problem_config_limits


_PROBLEM_LIMITS = problem_config_limits(build_config_values())
_ICPC_RUN_TWICE_PACKAGE = Path(
    "third_party/icpc-package-examples/sample-run-twice.zip"
)
_POLYGON_RUN_TWICE_PACKAGE = Path(
    "third_party/polygon-package-examples/sample-run-twice$linux.zip"
)


class TestLargePackageImport(unittest.TestCase):
    def test_reference_icpc_problem_package(self) -> None:
        package = _ICPC_RUN_TWICE_PACKAGE
        with tempfile.TemporaryDirectory(prefix="icpc-canary-") as temp_dir:
            workspace = Path(temp_dir)
            with ArchiveView(package, problem_archive_policy(256 * 1024 * 1024)) as archive:
                result = ICPCPackageImportService().import_package(
                    workspace,
                    package.name,
                    archive,
                    text_limit_bytes=256 * 1024,
                    statement_sample_max_bytes=32 * 1024,
                    problem_config_limits=_PROBLEM_LIMITS,
                )

            problem = json.loads((workspace / "config/problem.json").read_text(encoding="utf-8"))
            self.assertEqual(result["title"], "Sample Run Twice")
            self.assertEqual(problem["mode"], "interactive")
            self.assertEqual(problem["pass_limit"], 2)
            self.assertEqual(problem["time_limit_ms"], 2000)
            self.assertEqual(result["tests"]["total"], 6)

    def test_reference_polygon_problem_package(self) -> None:
        package = _POLYGON_RUN_TWICE_PACKAGE
        with tempfile.TemporaryDirectory(prefix="polygon-canary-") as temp_dir:
            workspace = Path(temp_dir)
            with ArchiveView(package, problem_archive_policy(256 * 1024 * 1024)) as archive:
                result = PolygonPackageImportService().import_package(
                    workspace,
                    package.name,
                    archive,
                    text_limit_bytes=256 * 1024,
                    statement_sample_max_bytes=32 * 1024,
                    problem_config_limits=_PROBLEM_LIMITS,
                )

            problem = json.loads((workspace / "config/problem.json").read_text(encoding="utf-8"))
            self.assertEqual(result["title"], "Sample Run Twice")
            self.assertEqual(problem["mode"], "interactive")
            self.assertEqual(problem["pass_limit"], 2)
            self.assertEqual(result["tests"]["total"], 6)

    def test_reference_polygon_contest_package(self) -> None:
        package = Path(
            "third_party/polygon-package-examples/sample-contest.zip"
        )

        problem_policy = problem_archive_policy(256 * 1024 * 1024)
        with tempfile.TemporaryDirectory(prefix="polygon-contest-canary-") as temp_dir:
            with ArchiveView(
                package,
                contest_archive_policy(26, 256 * 1024 * 1024),
            ) as archive:
                service = PolygonContestImportService()
                parsed = service.parse_package(
                    package.name,
                    archive,
                    problem_policy=problem_policy,
                    max_problems=26,
                )
                staged = service.stage_statement_sources(
                    archive,
                    parsed["statement_files"],
                    Path(temp_dir) / "statements",
                )
                statement_template = next(
                    row
                    for row in staged
                    if row["key"] == "statements/english/statements.ftl"
                )
                staged_template_text = statement_template["source_path"].read_text(
                    encoding="utf-8"
                )

        self.assertEqual(parsed["total_problems"], 4)
        self.assertEqual(parsed["title"], "Sample Contest")
        self.assertIn(r"\documentclass", staged_template_text)
