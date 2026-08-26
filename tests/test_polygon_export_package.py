import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from app.config import ConfigValues
from app.service.export.adapters.polygon import PolygonLinuxPackageAdapter
from app.service.problem_package.manifest import (
    NativePackageManifest,
    describe_file,
)
from app.service.problem_package.service import NativePackageReader
from app.service.problem_package.store import MaterializationRow


class TestPolygonExportPackage(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="polygon-export-package-"
        )
        self.root = Path(self._temporary_directory.name)
        self.values = ConfigValues(
            {
                "GENERAL_TIME_LIMIT_MIN_MS": 1,
                "GENERAL_TIME_LIMIT_MAX_MS": 60_000,
                "GENERAL_MEMORY_LIMIT_MIN_MB": 1,
                "GENERAL_MEMORY_LIMIT_MAX_MB": 8192,
                "GENERAL_PASS_LIMIT_MIN": 1,
                "GENERAL_PASS_LIMIT_MAX": 64,
                "AUX_DISPLAY_TEXT_LIMIT_BYTES": 64 * 1024,
            },
            normalizer=lambda raw: raw,
        )
        self.tex_compile = mock.Mock()

        def compile_pdf(entrypoint: Path) -> SimpleNamespace:
            pdf = entrypoint.with_suffix(".pdf")
            pdf.write_bytes(b"%PDF-1.4\npolygon\n")
            return SimpleNamespace(
                proc=SimpleNamespace(returncode=0, stderr="", stdout=""),
                pdf_path=pdf,
                log_text="",
            )

        self.tex_compile.compile_pdf.side_effect = compile_pdf

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_three_pass_package_preserves_full_polygon_run_count(self) -> None:
        reader = self._reader(pass_limit=3)
        target = self.root / "polygon-package"
        adapter = PolygonLinuxPackageAdapter(self.values, self.tex_compile)

        adapter.build(
            reader,
            target=target,
            canonical_problem_slug="owner/three-pass",
        )

        problem = ET.parse(target / "problem.xml").getroot()
        self.assertEqual(problem.attrib["short-name"], "three-pass")
        judging = cast(ET.Element, problem.find("judging"))
        self.assertEqual(judging.attrib["run-count"], "3")
        time_limit = cast(
            ET.Element,
            problem.find("judging/testset/time-limit"),
        )
        memory_limit = cast(
            ET.Element,
            problem.find("judging/testset/memory-limit"),
        )
        self.assertEqual(
            time_limit.text,
            "1750",
        )
        self.assertEqual(
            memory_limit.text,
            str(384 * 1024 * 1024),
        )
        multipass = cast(
            ET.Element,
            problem.find('properties/property[@name="multipass"]'),
        )
        self.assertEqual(multipass.attrib["value"], "true")
        self.assertEqual(len(problem.findall("assets/interactor/runs/run")), 3)
        self.assertEqual((target / "tests" / "01").read_bytes(), b"judge\n")
        self.assertEqual((target / "tests" / "01.a").read_bytes(), b"")
        self.assertEqual(
            (target / "statements" / "english" / "example.01").read_text(
                encoding="utf-8"
            ),
            "display\n",
        )
        properties = json.loads(
            (
                target
                / "statements"
                / "english"
                / "problem-properties.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(properties["name"], "Three Pass Problem")
        self.assertEqual(
            properties["sampleTests"],
            [{"input": "display\n", "output": ""}],
        )
        self.assertEqual(properties["timeLimit"], 1750)
        self.assertEqual(properties["memoryLimit"], 384 * 1024 * 1024)
        self.assertTrue(
            (
                target
                / "statements"
                / ".pdf"
                / "english"
                / "problem.pdf"
            ).is_file()
        )
        self.assertTrue((target / "files" / "interactor.cpp").is_file())

    def test_pass_fail_package_materializes_exact_checker_for_both_consumers(
        self,
    ) -> None:
        reader = self._reader(pass_limit=1)
        problem_config = {
            "time_limit_ms": 1750,
            "memory_limit_mb": 384,
            "mode": "pass-fail",
            "pass_limit": 1,
        }
        (reader.root / "config" / "problem.json").write_text(
            json.dumps(problem_config) + "\n",
            encoding="utf-8",
        )
        (reader.root / "config" / "build.json").write_text(
            json.dumps(
                {
                    "accepted_solution_source": "solutions/main.cpp",
                    "generator_sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        reader.manifest["mode"] = "pass-fail"
        answer_path = reader.root / "test-data" / "tests" / "001" / "answer"
        answer_path.write_bytes(b"answer\n")
        reader.manifest["tests"][0]["answer"] = describe_file(
            answer_path,
            root=reader.root,
        )
        target = self.root / "pass-fail-polygon-package"

        PolygonLinuxPackageAdapter(
            self.values,
            self.tex_compile,
        ).build(
            reader,
            target=target,
            canonical_problem_slug="owner/pass-fail",
        )

        exact_checker = (target / "check.cpp").read_text(encoding="utf-8")
        self.assertIn("std::ios::binary", exact_checker)
        self.assertEqual(
            (target / "files" / "check.cpp").read_text(encoding="utf-8"),
            exact_checker,
        )
        problem = ET.parse(target / "problem.xml").getroot()
        checker_source = cast(
            ET.Element,
            problem.find("assets/checker/source"),
        )
        self.assertEqual(checker_source.attrib["path"], "files/check.cpp")
        self.assertEqual((target / "tests" / "01.a").read_bytes(), b"answer\n")

    def _reader(self, *, pass_limit: int) -> NativePackageReader:
        package_root = self.root / "native"
        for directory in (
            "config",
            "tests",
            "solutions",
            "interactors",
            "statement",
            "statement-sections/english",
            "statement-build/english",
            "third_party/testlib",
        ):
            (package_root / directory).mkdir(parents=True, exist_ok=True)
        (package_root / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "time_limit_ms": 1750,
                    "memory_limit_mb": 384,
                    "mode": "interactive",
                    "pass_limit": pass_limit,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (package_root / "config" / "build.json").write_text(
            json.dumps(
                {
                    "accepted_solution_source": "solutions/main.cpp",
                    "interactor_source": "interactors/interactor.cpp",
                    "generator_sources": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (package_root / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "tests": [
                        {
                            "id": "001",
                            "kind": "manual",
                            "sample": True,
                            "sample_input": "display\n",
                            "sample_output": "",
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (package_root / "solutions" / "main.cpp").write_text(
            "int main() {}\n",
            encoding="utf-8",
        )
        (package_root / "interactors" / "interactor.cpp").write_text(
            "int main() {}\n",
            encoding="utf-8",
        )
        (package_root / "third_party" / "testlib" / "testlib.h").write_text(
            "// testlib\n",
            encoding="utf-8",
        )
        for filename in ("statements.ftl", "problem.tex", "olymp.sty"):
            (package_root / "statement" / filename).write_text(
                f"% {filename}\n",
                encoding="utf-8",
            )
        sections = package_root / "statement-sections" / "english"
        (sections / "name.tex").write_text(
            "Three Pass Problem\n",
            encoding="utf-8",
        )
        (sections / "legend.tex").write_text("Legend\n", encoding="utf-8")
        statement_build = package_root / "statement-build" / "english"
        (statement_build / "statements.tex").write_text(
            "statement\n",
            encoding="utf-8",
        )
        (statement_build / "problem.tex").write_text(
            "problem\n",
            encoding="utf-8",
        )
        test_root = package_root / "test-data" / "tests" / "001"
        test_root.mkdir(parents=True)
        input_path = test_root / "input"
        input_path.write_bytes(b"judge\n")
        display_path = test_root / "sample-input"
        display_path.write_bytes(b"display\n")

        materialization: MaterializationRow = {
            "id": "pm-polygon",
            "problem_id": 1,
            "source_commit": "a" * 40,
            "revision_number": 4,
            "source_digest": "b" * 64,
            "archive_rel_path": "materializations/polygon.zip",
            "archive_sha256": "c" * 64,
            "archive_size_bytes": 1,
            "verification_id": "ver-polygon",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "checked_at": "2026-01-01T00:00:00Z",
            "unavailable_reason": "",
        }
        manifest: NativePackageManifest = {
            "source_commit": materialization["source_commit"],
            "revision_number": materialization["revision_number"],
            "source_digest": materialization["source_digest"],
            "mode": "interactive",
            "pass_limit": pass_limit,
            "solutions": [
                {
                    "source_path": "solutions/main.cpp",
                    "expected_behavior": "accepted",
                }
            ],
            "tests": [
                {
                    "id": "001",
                    "kind": "manual",
                    "sample": True,
                    "input": describe_file(input_path, root=package_root),
                    "sample_input": describe_file(
                        display_path,
                        root=package_root,
                    ),
                }
            ],
        }
        return NativePackageReader(
            native_package=materialization,
            root=package_root,
            manifest=manifest,
        )


if __name__ == "__main__":
    unittest.main()
