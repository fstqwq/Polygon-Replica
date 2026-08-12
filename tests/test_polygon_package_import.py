import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from app.service.importing.polygon import PolygonPackageImportService
from app.service.problem.test_spec import load_tests_spec
from tests.archive_support import import_problem_package
from tests.package_builders import polygon_problem_package


class TestPolygonPackageImport(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="polygon-import-")
        self.workspace = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _workspace_path(self) -> Path:
        return self.workspace

    def test_unconsumed_member_does_not_spend_expansion_budget(self) -> None:
        payload = io.BytesIO(polygon_problem_package())
        with zipfile.ZipFile(payload, "a", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("unused/large.bin", b"x" * (1024 * 1024))

        result = import_problem_package(
            PolygonPackageImportService(),
            self.workspace,
            "polygon.zip",
            payload.getvalue(),
            max_expanded_bytes=64 * 1024,
        )
        self.assertEqual(result["tests"]["total"], 1)
        self.assertEqual(
            result["components"]["testlib_source"],
            "third_party/testlib/testlib.h",
        )
        self.assertTrue(
            (self.workspace / "third_party/testlib/testlib.h").is_file()
        )

    def test_import_generated_python_generator_keeps_generator_source(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="python-generator">
  <names>
    <name language="english" value="Python Generator Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>1048576</memory-limit>
      <tests>
        <test method="generated" cmd="gen.py 7"/>
      </tests>
    </testset>
  </judging>
  <files>
    <executables>
      <executable>
        <source path="files/gen.py" type="python.3"/>
      </executable>
    </executables>
  </files>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("files/gen.py", "print(7)\n")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "python-generator.zip", payload.getvalue()
        )
        tests_summary = result.get("tests") if isinstance(result.get("tests"), dict) else {}
        self.assertEqual(int(tests_summary.get("gen") or 0), 1)
        self.assertEqual(int(tests_summary.get("generated_fallback_to_manual") or 0), 0)
        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(list(build_cfg.get("generator_sources") or []), ["generators/gen.py"])
        self.assertEqual((ws / "generators" / "gen.py").read_text(encoding="utf-8"), "print(7)\n")
        problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(problem_cfg["memory_limit_mb"], 1)

    def test_import_multipass_property_without_explicit_pass_limit_fails(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="mp-override">
  <names>
    <name language="english" value="Multipass Overrides Interactor"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
  <assets>
    <interactor>
      <source path="files/interactor.cpp" type="cpp.g++17"/>
    </interactor>
  </assets>
  <properties>
    <property name="multi-pass" value="true"/>
  </properties>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "1\n")
            zf.writestr("files/interactor.cpp", "int main(){return 0;}\n")

        service = PolygonPackageImportService()
        with self.assertRaisesRegex(ValueError, "missing explicit pass limit"):
            import_problem_package(
                service, ws, "mp-override.zip", payload.getvalue()
            )

    def test_import_can_normalize_windows_newlines_for_test_data(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="win-data">
  <names>
    <name language="english" value="Windows Data Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", b"1 2\r\n3 4\r\n")
            zf.writestr("tests/01.a", b"5\r\n6\r\n")

        service = PolygonPackageImportService()
        import_problem_package(
            service,
            ws,
            "windows-data.zip",
            payload.getvalue(),
            normalize_test_data_newlines=True,
        )
        self.assertEqual((ws / "tests" / "manual" / "001.in").read_bytes(), b"1 2\n3 4\n")
        spec = json.loads((ws / "tests" / "spec.json").read_text(encoding="utf-8"))
        tests = spec.get("tests") if isinstance(spec, dict) else []
        self.assertEqual(str(tests[0].get("sample_output") or ""), "5\n6\n")
        self.assertFalse((ws / "tests" / "answers").exists())

    def test_import_rejects_non_utf8_manual_test_payload(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="bad-utf8">
  <names>
    <name language="english" value="Bad UTF-8 Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", b"\xff\xfe\xfd")

        service = PolygonPackageImportService()
        with self.assertRaisesRegex(ValueError, "manual test input must be utf-8 text: tests/01"):
            import_problem_package(
                service, ws, "bad-utf8.zip", payload.getvalue()
            )

    def test_import_accepts_root_level_statement_resources(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="root-style">
  <names>
    <name language="english" value="Root Style Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
  <files>
    <resources>
      <file path="statements.ftl"/>
      <file path="problem.tex"/>
      <file path="olymp.sty"/>
      <file path="testlib.h"/>
    </resources>
  </files>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("statements.ftl", "ROOT_FTL_TEMPLATE\n")
            zf.writestr("problem.tex", "ROOT_PROBLEM_TEMPLATE\n")
            zf.writestr("olymp.sty", "ROOT_OLYMP_STYLE\n")
            zf.writestr("testlib.h", "// ROOT TESTLIB\n")
            zf.writestr("tests/01", "1\n")
            zf.writestr("statement-sections/english/legend.tex", "Root legend.\n")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "root-level.zip", payload.getvalue()
        )
        self.assertEqual(str(result.get("title") or ""), "Root Style Import")
        self.assertEqual((ws / "statement" / "statements.ftl").read_text(encoding="utf-8"), "ROOT_FTL_TEMPLATE\n")
        self.assertEqual((ws / "statement" / "problem.tex").read_text(encoding="utf-8"), "ROOT_PROBLEM_TEMPLATE\n")
        self.assertEqual((ws / "statement" / "olymp.sty").read_text(encoding="utf-8"), "ROOT_OLYMP_STYLE\n")
        upstream_testlib = Path("third_party/upstream/testlib/testlib.h").read_text(encoding="utf-8")
        workspace_testlib = (ws / "third_party" / "testlib" / "testlib.h").read_text(encoding="utf-8")
        self.assertEqual(workspace_testlib, upstream_testlib)
        self.assertNotEqual(workspace_testlib, "// ROOT TESTLIB\n")
        self.assertFalse((ws / "statement-sections" / "english" / "input.tex").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "output.tex").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "notes.tex").exists())

    def test_import_missing_statement_sections_does_not_create_default_section_files(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="no-sections">
  <names>
    <name language="english" value="No Sections Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "1\n")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "missing-sections.zip", payload.getvalue()
        )
        self.assertEqual(str(result.get("title") or ""), "No Sections Import")
        self.assertFalse((ws / "statement-sections" / "english" / "legend.tex").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "input.tex").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "output.tex").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "notes.tex").exists())

    def test_import_non_english_statement_sections_reports_language_warning(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="non-english">
  <names>
    <name language="english" value="Non English Sections"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "1\n")
            zf.writestr("statement-sections/russian/legend.tex", "Legend RU\n")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "non-english-sections.zip", payload.getvalue()
        )
        statement_summary = result.get("statement") if isinstance(result.get("statement"), dict) else {}
        self.assertEqual(str(statement_summary.get("language") or ""), "russian")
        self.assertIn("english not found", str(statement_summary.get("language_warning") or ""))
        self.assertTrue((ws / "statement-sections" / "russian" / "legend.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_import_legacy_statement_assets_merge_to_shared_root_with_conflict_warning(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="shared-assets">
  <names>
    <name language="english" value="Shared Assets"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "1\n")
            zf.writestr("statement-sections/english/legend.tex", "Legend EN\n")
            zf.writestr("statement-sections/chinese/legend.tex", "Legend ZH\n")
            zf.writestr("statement-sections/english/diagram.png", b"EN")
            zf.writestr("statement-sections/chinese/diagram.png", b"ZH")
            zf.writestr("statement-sections/english/shared.png", b"SAME")
            zf.writestr("statement-sections/chinese/shared.png", b"SAME")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "shared-assets.zip", payload.getvalue()
        )

        self.assertEqual((ws / "statement-assets" / "diagram.png").read_bytes(), b"EN")
        self.assertEqual((ws / "statement-assets" / "diagram-zh.png").read_bytes(), b"ZH")
        self.assertEqual((ws / "statement-assets" / "shared.png").read_bytes(), b"SAME")
        self.assertFalse((ws / "statement-assets" / "shared-zh.png").exists())
        warnings = [str(item) for item in (result.get("warnings") or [])]
        self.assertTrue(any("statement-sections/chinese/diagram.png" in item for item in warnings))
        self.assertTrue(any("statement-assets/diagram-zh.png" in item for item in warnings))
        self.assertFalse(any("shared.png" in item for item in warnings))

    def test_import_statement_examples_override_sample_io_and_enable_validate(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="example-override">
  <names>
    <name language="english" value="Example Override"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
        <test method="manual"/>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.xml", xml)
            zf.writestr("tests/01", "raw input 1\n")
            zf.writestr("tests/01.a", "raw answer 1\n")
            zf.writestr("tests/02", "raw input 2\n")
            zf.writestr("tests/02.a", "raw answer 2\n")
            zf.writestr("tests/03", "raw input 3\n")
            zf.writestr("tests/03.a", "raw answer 3\n")
            zf.writestr("statement-sections/english/example.01", "example input 1\n")
            zf.writestr("statement-sections/english/example.01.a", "example output 1\n")
            zf.writestr("statement-sections/english/example.02", "example input 2\n")
            zf.writestr("statement-sections/english/example.02.a", "example output 2\n")

        service = PolygonPackageImportService()
        result = import_problem_package(
            service, ws, "example-override.zip", payload.getvalue()
        )
        self.assertEqual(str(result.get("title") or ""), "Example Override")

        tests = load_tests_spec(
            ws / "tests" / "spec.json",
            document_max_bytes=256 * 1024,
            sample_max_bytes=32 * 1024,
        )
        self.assertEqual(len(tests), 3)
        self.assertEqual(str(tests[0].get("sample_input") or ""), "example input 1\n")
        self.assertEqual(str(tests[0].get("sample_output") or ""), "example output 1\n")
        self.assertTrue(bool(tests[0].get("sample_output_validate")))
        self.assertEqual(str(tests[1].get("sample_input") or ""), "")
        self.assertEqual(str(tests[1].get("sample_output") or ""), "")
        self.assertEqual(str(tests[2].get("sample_input") or ""), "example input 2\n")
        self.assertEqual(str(tests[2].get("sample_output") or ""), "example output 2\n")
        self.assertTrue(bool(tests[2].get("sample_output_validate")))
        self.assertFalse((ws / "tests" / "answers").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "example.01").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "example.01.a").exists())

    def test_import_interactive_package_drops_answer_payloads(self) -> None:
        workspace = self._workspace_path()
        payload = io.BytesIO()
        problem_xml = """<?xml version="1.0" encoding="UTF-8"?>
<problem short-name="interactive-answer">
  <names>
    <name language="english" value="Interactive Answer Import"/>
  </names>
  <judging run-count="1">
    <testset>
      <time-limit>1000</time-limit>
      <memory-limit>268435456</memory-limit>
      <input-path-pattern>tests/%02d</input-path-pattern>
      <answer-path-pattern>tests/%02d.a</answer-path-pattern>
      <tests>
        <test method="manual" sample="true"/>
      </tests>
    </testset>
  </judging>
  <assets>
    <interactor>
      <source path="files/interactor.cpp" type="cpp.g++17"/>
    </interactor>
  </assets>
</problem>
"""
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("problem.xml", problem_xml)
            package.writestr("tests/01", "input\n")
            package.writestr("tests/01.a", "team output\n")
            package.writestr("files/interactor.cpp", "int main(){return 0;}\n")

        import_problem_package(
            PolygonPackageImportService(),
            workspace,
            "interactive-answer.zip",
            payload.getvalue(),
        )

        problem = json.loads((workspace / "config/problem.json").read_text(encoding="utf-8"))
        tests = load_tests_spec(
            workspace / "tests/spec.json",
            document_max_bytes=256 * 1024,
            sample_max_bytes=32 * 1024,
        )
        self.assertEqual(problem["mode"], "interactive")
        self.assertEqual(problem["pass_limit"], 1)
        self.assertEqual(tests[0]["sample_output"], "team output\n")
        self.assertFalse((workspace / "tests/answers").exists())
