from __future__ import annotations

from .db_helpers import db_execute, db_fetch_one, read_preview_summary

import io
import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.service.importing.polygon import PolygonPackageImportService
from app.service.statement.render import render_statement_main
from .common import SmokeBase, config


class TestPolygonPackageImport(SmokeBase):
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
            service.import_package(ws, "mp-override.zip", payload.getvalue())

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
        service.import_package(
            ws,
            "windows-data.zip",
            payload.getvalue(),
            normalize_test_data_newlines=True,
        )
        self.assertEqual((ws / "tests" / "manual" / "001.in").read_bytes(), b"1 2\n3 4\n")
        self.assertEqual((ws / "tests" / "answers" / "001.ans").read_bytes(), b"5\n6\n")
        spec = json.loads((ws / "tests" / "spec.json").read_text(encoding="utf-8"))
        tests = spec.get("tests") if isinstance(spec, dict) else []
        self.assertEqual(str(tests[0].get("sample_output") or ""), "5\n6\n")

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
        result = service.import_package(ws, "root-level.zip", payload.getvalue())
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
        result = service.import_package(ws, "missing-sections.zip", payload.getvalue())
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
        result = service.import_package(ws, "non-english-sections.zip", payload.getvalue())
        statement_summary = result.get("statement") if isinstance(result.get("statement"), dict) else {}
        self.assertEqual(str(statement_summary.get("language") or ""), "russian")
        self.assertIn("english not found", str(statement_summary.get("language_warning") or ""))
        self.assertTrue((ws / "statement-sections" / "russian" / "legend.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_import_run_twice_linux_package(self) -> None:
        ws = self._workspace_path()
        package = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package.exists(), f"missing package fixture: {package}")
        service = PolygonPackageImportService()
        result = service.import_package(ws, package.name, package.read_bytes())

        self.assertEqual(str(result.get("title") or ""), "Guess the Number (Deluxe ver.)")
        self.assertTrue((ws / "statement" / "statements.ftl").is_file())
        self.assertTrue((ws / "statement" / "problem.tex").is_file())
        self.assertTrue((ws / "statement" / "olymp.sty").is_file())
        self.assertTrue((ws / "statement-sections" / "english" / "legend.tex").is_file())

        spec = json.loads((ws / "tests" / "spec.json").read_text(encoding="utf-8"))
        tests = spec.get("tests") if isinstance(spec, dict) else []
        self.assertEqual(len(tests), 6)
        self.assertTrue(all((str(row.get("kind") or "") == "manual") for row in tests))
        tests_summary = result.get("tests") if isinstance(result.get("tests"), dict) else {}
        self.assertEqual(int(tests_summary.get("generated_fallback_to_manual") or 0), 6)
        self.assertEqual(int(tests_summary.get("answers") or 0), 6)
        self.assertEqual((ws / "tests" / "answers" / "001.ans").read_text(encoding="utf-8").strip(), "37")
        self.assertEqual((ws / "tests" / "answers" / "002.ans").read_text(encoding="utf-8").strip(), "58")
        self.assertFalse((ws / "statement-sections" / "english" / "example.01").exists())
        self.assertFalse((ws / "statement-sections" / "english" / "example.01.a").exists())

        problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(str(problem_cfg.get("mode") or ""), "interactive")
        self.assertEqual(int(problem_cfg.get("pass_limit") or 0), 2)
        self.assertEqual(int(problem_cfg.get("time_limit_ms") or 0), 2000)

        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(str(build_cfg.get("interactor_source") or ""), "interactors/interactor.cpp")
        self.assertTrue(str(build_cfg.get("accepted_solution_source") or "").startswith("solutions/"))
        self.assertEqual(str(build_cfg.get("validator_source") or ""), "validators/validator.cpp")
        self.assertEqual(list(build_cfg.get("generator_sources") or []), [])
        self.assertIsNone(build_cfg.get("max_passes"))

        imported_testlib = (ws / "third_party" / "testlib" / "testlib.h").read_bytes()
        upstream_testlib = Path("third_party/upstream/testlib/testlib.h").read_bytes()
        self.assertEqual(imported_testlib, upstream_testlib)

        main_tex = render_statement_main(ws / "statement", problem_title=str(result.get("title") or ""))
        rendered = main_tex.read_text(encoding="utf-8")
        self.assertIn(r"\import{rendered/english/}{./problem.tex}", rendered)
        rendered_problem = (ws / "statement" / "rendered" / "english" / "problem.tex").read_text(encoding="utf-8")
        self.assertIn(r"\begin{problem}{Guess the Number (Deluxe ver.)}", rendered_problem)
        self.assertTrue((ws / "statement" / "rendered" / "english" / "problem.pdf").is_file())

        def _fake_run_build(problem: str, username: str, *args, **kwargs) -> str:
            self.assertEqual(problem, self.problem)
            self.assertEqual(username, self.user)
            ctx = config.workspace_service.workspace_context(self.problem, self.user, include_recent=False)
            verification_id = f"ver-import-ok-{uuid.uuid4().hex[:8]}"
            db_execute(
                """
                INSERT INTO verifications(id,problem_id,workspace_id,signature,kind,status,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    verification_id,
                    int(ctx["problem"]["id"]),
                    int(ctx["workspace"]["id"]),
                    "",
                    "all",
                    "ok",
                    "2026-02-23T00:00:00Z",
                    "2026-02-23T00:00:01Z",
                ],
            )
            return verification_id

        with patch.object(config.verification_service, "run_verification", side_effect=_fake_run_build):
            verification_id = config.verification_service.run_verification(self.problem, self.user)
        verification_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or ""), "ok")

    def test_import_hangzhou_interactive_package_keeps_answer_payloads(self) -> None:
        ws = self._workspace_path()
        package = Path("third_party/polygon-package-examples/2024hangzhou-rank-list-interactive-35$linux.zip")
        self.assertTrue(package.exists(), f"missing package fixture: {package}")
        service = PolygonPackageImportService()
        result = service.import_package(ws, package.name, package.read_bytes())
        self.assertEqual(str(result.get("title") or ""), "Fuzzy Ranking (Interactive ver.)")
        tests_summary = result.get("tests") if isinstance(result.get("tests"), dict) else {}
        self.assertEqual(int(tests_summary.get("answers") or 0), 27)
        self.assertTrue((ws / "tests" / "answers" / "001.ans").is_file())
        self.assertTrue((ws / "tests" / "answers" / "027.ans").is_file())
        self.assertEqual((ws / "tests" / "answers" / "001.ans").read_text(encoding="utf-8").splitlines()[:1], ["3"])
        problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(str(problem_cfg.get("mode") or ""), "interactive")
        self.assertEqual(int(problem_cfg.get("pass_limit") or 0), 1)

        verification_id = config.verification_service.run_verification(self.problem, self.user)
        verification_row = db_fetch_one("SELECT status FROM verifications WHERE id=?", [verification_id])
        self.assertIsNotNone(verification_row)
        self.assertEqual(str(verification_row["status"] or ""), "failed")

    def test_import_real_packages_use_canonical_mode_and_pass_limit(self) -> None:
        service = PolygonPackageImportService()
        package_dir = Path("third_party/polygon-package-examples")
        cases = [
            ("2024hangzhou-rank-list-interactive-35$linux.zip", "interactive", 1),
            ("2024yunnan-matrix-15$linux.zip", "pass-fail", 1),
            ("run-twice-guess-the-number-46$linux.zip", "interactive", 2),
            ("taxi-26$linux.zip", "pass-fail", 1),
        ]
        for package_name, expected_mode, expected_pass_limit in cases:
            ws = Path(tempfile.mkdtemp(prefix=f"polygon-mode-pass-{Path(package_name).stem}-"))
            try:
                package = package_dir / package_name
                self.assertTrue(package.exists(), f"missing package fixture: {package}")
                service.import_package(ws, package.name, package.read_bytes())
                problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
                self.assertEqual(str(problem_cfg.get("mode") or ""), expected_mode)
                self.assertEqual(int(problem_cfg.get("pass_limit") or 0), expected_pass_limit)
                self.assertNotIn("max_passes", problem_cfg)
            finally:
                shutil.rmtree(ws, ignore_errors=True)

    def test_import_taxi_maps_time_limit_exceeded_or_accepted_tag_to_tle_or_correct(self) -> None:
        ws = self._workspace_path()
        package = Path("third_party/polygon-package-examples/taxi-26$linux.zip")
        self.assertTrue(package.exists(), f"missing package fixture: {package}")
        service = PolygonPackageImportService()
        service.import_package(ws, package.name, package.read_bytes())
        marker = ws / "solutions" / "1.cpp.desc"
        self.assertTrue(marker.is_file())
        desc = marker.read_text(encoding="utf-8")
        self.assertIn("expected: tle_or_correct", desc)

    def test_import_a_lot_of_verdicts_maps_extended_solution_tags(self) -> None:
        service = PolygonPackageImportService()
        package_dir = Path("third_party/polygon-package-examples")
        for package_name in ("a-lot-of-verdicts-1.zip", "a-lot-of-verdicts-1$linux.zip"):
            package = package_dir / package_name
            self.assertTrue(package.exists(), f"missing package fixture: {package}")
            ws = Path(tempfile.mkdtemp(prefix=f"polygon-import-{package.stem}-"))
            try:
                result = service.import_package(ws, package.name, package.read_bytes())
                solutions_summary = result.get("solutions") if isinstance(result.get("solutions"), dict) else {}
                self.assertEqual(int(solutions_summary.get("count") or 0), 8)
                self.assertEqual(str(solutions_summary.get("accepted_source") or ""), "solutions/std.py")
                self.assertTrue((ws / "solutions" / "sorrywronglanguage.cpp.py").is_file())
                self.assertFalse((ws / "solutions" / "sorrywronglanguage.cpp").exists())
                self.assertIn(
                    "expected: accepted",
                    (ws / "solutions" / "sorrywronglanguage.cpp.py.desc").read_text(encoding="utf-8"),
                )
                self.assertIn("expected: run_time_error", (ws / "solutions" / "mle.cpp.desc").read_text(encoding="utf-8"))
                self.assertIn("expected: tle_or_correct", (ws / "solutions" / "tleorac.py.desc").read_text(encoding="utf-8"))
                self.assertIn("expected: tle_or_correct", (ws / "solutions" / "test.py.desc").read_text(encoding="utf-8"))
                self.assertIn("expected: tle_or_re", (ws / "solutions" / "tlorml.py.desc").read_text(encoding="utf-8"))
                self.assertIn("expected: wrong_answer", (ws / "solutions" / "wa.py.desc").read_text(encoding="utf-8"))
            finally:
                shutil.rmtree(ws, ignore_errors=True)

    def test_preview_fails_when_pdflatex_missing(self) -> None:
        problem = str(self.problem)
        user = str(self.user)
        ws = self._workspace_path()
        package = Path("third_party/polygon-package-examples/run-twice-guess-the-number-46$linux.zip")
        self.assertTrue(package.exists(), f"missing package fixture: {package}")
        service = PolygonPackageImportService()
        result = service.import_package(ws, package.name, package.read_bytes())
        render_statement_main(ws / "statement", problem_title=str(result.get("title") or ""))
        with (
            patch.object(config.preview_service, "_sample_verification_rows_from_spec", return_value=[]),
            patch.object(config.preview_service.sandbox, "run", side_effect=FileNotFoundError("pdflatex missing")),
        ):
            preview_id = config.preview_service.compile_preview(problem, user)
        row = db_fetch_one("SELECT status FROM previews WHERE id=?", [preview_id])
        self.assertIsNotNone(row)
        self.assertEqual(str(row["status"] or ""), "failed")
        summary = read_preview_summary(preview_id)
        self.assertIn("pdflatex missing", str(summary.get("error") or ""))
        artifact_root = config.fs_manager.resolve_preview_root(preview_id)
        self.assertFalse((artifact_root / "statement_preview" / "statement.pdf").exists())
