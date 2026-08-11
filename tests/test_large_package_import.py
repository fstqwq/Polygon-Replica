from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.service.importing.contest import PolygonContestImportService
from app.service.importing.archive import (
    ArchiveView,
    contest_archive_policy,
    problem_archive_policy,
)
from app.service.importing.icpc import ICPCPackageImportService
from app.service.importing.polygon import PolygonPackageImportService


class TestLargePackageImport(unittest.TestCase):
    def test_reference_icpc_problem_package(self) -> None:
        package = Path("third_party/icpc-package-examples/ecf50-prac-a.zip")
        with tempfile.TemporaryDirectory(prefix="icpc-canary-") as temp_dir:
            workspace = Path(temp_dir)
            with ArchiveView(package, problem_archive_policy(256 * 1024 * 1024)) as archive:
                result = ICPCPackageImportService().import_package(
                    workspace,
                    package.name,
                    archive,
                    text_limit_bytes=256 * 1024,
                )

            problem = json.loads((workspace / "config/problem.json").read_text(encoding="utf-8"))
            self.assertEqual(problem["mode"], "interactive")
            self.assertEqual(problem["pass_limit"], 2)
            self.assertEqual(problem["time_limit_ms"], 2000)
            self.assertGreater(result["tests"]["total"], 0)

    def test_reference_polygon_problem_package(self) -> None:
        package = Path(
            "third_party/polygon-package-examples/"
            "run-twice-guess-the-number-46$linux.zip"
        )
        with tempfile.TemporaryDirectory(prefix="polygon-canary-") as temp_dir:
            workspace = Path(temp_dir)
            with ArchiveView(package, problem_archive_policy(256 * 1024 * 1024)) as archive:
                result = PolygonPackageImportService().import_package(
                    workspace,
                    package.name,
                    archive,
                    text_limit_bytes=256 * 1024,
                )

            problem = json.loads((workspace / "config/problem.json").read_text(encoding="utf-8"))
            self.assertEqual(result["title"], "Guess the Number (Deluxe ver.)")
            self.assertEqual(problem["mode"], "interactive")
            self.assertEqual(problem["pass_limit"], 2)
            self.assertEqual(result["tests"]["total"], 6)

    def test_reference_polygon_contest_package(self) -> None:
        package = Path(
            "third_party/polygon-package-examples/contest/contest-55738.zip"
        )

        problem_policy = problem_archive_policy(256 * 1024 * 1024)
        with ArchiveView(
            package,
            contest_archive_policy(26, 256 * 1024 * 1024),
        ) as archive:
            parsed = PolygonContestImportService().parse_package(
                package.name,
                archive,
                problem_policy=problem_policy,
                max_problems=26,
            )

        self.assertEqual(parsed["total_problems"], 4)
        self.assertEqual(parsed["default_language"], "english")
        self.assertIn("ICPC Asia East Continent Final", parsed["title"])
