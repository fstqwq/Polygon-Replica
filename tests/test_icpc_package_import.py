from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path

from app.service.importing.icpc import ICPCPackageImportService
from .common import SmokeBase


class TestICPCPackageImport(SmokeBase):
    def test_import_reference_icpc_packages(self) -> None:
        service = ICPCPackageImportService()
        package_dir = Path("third_party/icpc-package-examples")
        expected = {
            "ecf50-prac-a.zip": ("interactive", 2, 2000, 0),
            "ecf50-prac-b.zip": ("interactive", 1, 6000, 0),
            "ecf50-prac-c.zip": ("pass-fail", 1, 2000, 4),
            "ecf50-prac-d.zip": ("pass-fail", 1, 2000, 2),
        }
        for package_name, (mode, pass_limit, time_limit_ms, sample_count) in expected.items():
            package = package_dir / package_name
            self.assertTrue(package.is_file(), f"missing ICPC package fixture: {package}")
            with tempfile.TemporaryDirectory(prefix=f"icpc-import-{package.stem}-") as td:
                ws = Path(td)
                result = service.import_package(ws, package.name, package.read_bytes())
                problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
                self.assertEqual(problem_cfg.get("mode"), mode, package_name)
                self.assertEqual(problem_cfg.get("pass_limit"), pass_limit, package_name)
                self.assertEqual(problem_cfg.get("time_limit_ms"), time_limit_ms, package_name)
                self.assertTrue((ws / "tests" / "spec.json").is_file(), package_name)
                self.assertGreater(int(result.get("tests", {}).get("total") or 0), 0, package_name)
                self.assertEqual(int(result.get("tests", {}).get("sample") or 0), sample_count, package_name)

    def test_import_icpc_package_basic_layout(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "roundtrip/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Roundtrip ICPC",
                        "validation: custom",
                    ]
                )
                + "\n",
            )
            zf.writestr("roundtrip/statement/statements.ftl", "ROUNDTRIP_FTL\n")
            zf.writestr(
                "roundtrip/statement/problem.en.tex",
                (
                    r"\begin{problem}{Roundtrip ICPC}{stdin}{stdout}{2 seconds}{256 megabytes}" + "\n"
                    "Legend.\n"
                    r"\InputFile" + "\n"
                    "Input.\n"
                    r"\OutputFile" + "\n"
                    "Output.\n"
                    r"\end{problem}" + "\n"
                ),
            )
            zf.writestr("roundtrip/data/secret/001.in", "1\n")
            zf.writestr("roundtrip/data/secret/001.ans", "1\n")
            zf.writestr("roundtrip/data/sample/1.in", "1\n")
            zf.writestr("roundtrip/data/sample/1.ans", "1\n")
            zf.writestr("roundtrip/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("roundtrip/submissions/wrong_answer/wa.cpp", "int main(){return 0;}\n")
            zf.writestr("roundtrip/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("roundtrip/output_validator/checker.cpp", "int main(){return 0;}\n")

        service = ICPCPackageImportService()
        result = service.import_package(ws, "roundtrip.zip", payload.getvalue())
        self.assertEqual(str(result.get("title") or ""), "Roundtrip ICPC")
        self.assertEqual((ws / "statement" / "statements.ftl").read_text(encoding="utf-8"), "ROUNDTRIP_FTL\n")

        spec = json.loads((ws / "tests" / "spec.json").read_text(encoding="utf-8"))
        tests = spec.get("tests") if isinstance(spec, dict) else []
        self.assertEqual(len(tests), 1)
        self.assertEqual(str(tests[0].get("id") or ""), "001")
        self.assertEqual(str(tests[0].get("kind") or ""), "manual")
        self.assertTrue(bool(tests[0].get("sample")))
        self.assertEqual(str(tests[0].get("sample_output") or ""), "1\n")
        self.assertEqual((ws / "tests" / "manual" / "001.in").read_text(encoding="utf-8"), "1\n")
        self.assertFalse((ws / "tests" / "answers").exists())

        problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(str(problem_cfg.get("mode") or ""), "pass-fail")
        self.assertEqual(int(problem_cfg.get("pass_limit") or 0), 1)
        self.assertEqual(int(problem_cfg.get("time_limit_ms") or 0), 2000)
        self.assertEqual(int(problem_cfg.get("memory_limit_mb") or 0), 1024)

        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertTrue(str(build_cfg.get("accepted_solution_source") or "").startswith("solutions/"))
        self.assertEqual(str(build_cfg.get("validator_source") or ""), "validators/validator.cpp")
        self.assertEqual(str(build_cfg.get("checker_source") or ""), "checkers/checker.cpp")

    def test_import_icpc_package_interactive_type_sets_interactor_mode(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "interactive/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Interactive ICPC",
                        "validation: custom interactive",
                    ]
                )
                + "\n",
            )
            zf.writestr("interactive/data/secret/001.in", "1\n")
            zf.writestr("interactive/data/secret/001.ans", "1\n")
            zf.writestr("interactive/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("interactive/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("interactive/output_validator/interactor.cpp", "int main(){return 0;}\n")

        service = ICPCPackageImportService()
        result = service.import_package(ws, "interactive.zip", payload.getvalue())
        self.assertEqual(str(result.get("title") or ""), "Interactive ICPC")

        problem_cfg = json.loads((ws / "config" / "problem.json").read_text(encoding="utf-8"))
        self.assertEqual(str(problem_cfg.get("mode") or ""), "interactive")
        self.assertEqual(int(problem_cfg.get("pass_limit") or 0), 1)

        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(str(build_cfg.get("interactor_source") or ""), "interactors/interactor.cpp")
        self.assertFalse(bool(str(build_cfg.get("checker_source") or "").strip()))

    def test_import_icpc_package_without_accepted_submission_does_not_create_fallback_solution(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "noaccepted/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: No Accepted",
                        "validation: custom",
                    ]
                )
                + "\n",
            )
            zf.writestr("noaccepted/data/secret/001.in", "1\n")
            zf.writestr("noaccepted/submissions/wrong_answer/wa.cpp", "int main(){return 0;}\n")
            zf.writestr("noaccepted/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("noaccepted/output_validator/checker.cpp", "int main(){return 0;}\n")

        service = ICPCPackageImportService()
        result = service.import_package(ws, "noaccepted.zip", payload.getvalue())
        self.assertEqual(str(result.get("title") or ""), "No Accepted")

        self.assertFalse((ws / "solutions" / "accepted.cpp").exists())
        self.assertFalse((ws / "solutions" / "accepted.cpp.desc").exists())
        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertFalse(bool(str(build_cfg.get("accepted_solution_source") or "").strip()))

    def test_import_icpc_non_english_sections_reports_language_warning(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "lang/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Non English ICPC",
                        "validation: custom",
                    ]
                )
                + "\n",
            )
            zf.writestr("lang/data/secret/001.in", "1\n")
            zf.writestr("lang/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/output_validator/checker.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/statement-sections/russian/legend.tex", "Legend RU\n")

        service = ICPCPackageImportService()
        result = service.import_package(ws, "lang.zip", payload.getvalue())
        statement_summary = result.get("statement") if isinstance(result.get("statement"), dict) else {}
        self.assertEqual(str(statement_summary.get("language") or ""), "russian")
        self.assertIn("english not found", str(statement_summary.get("language_warning") or ""))
        self.assertTrue((ws / "statement-sections" / "russian" / "legend.tex").is_file())
        self.assertFalse((ws / "statement-sections" / "english").exists())

    def test_import_icpc_legacy_statement_assets_merge_to_shared_root_with_conflict_warning(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "lang/problem.yaml",
                "\n".join(
                    [
                        "problem_format_version: 2025-09",
                        "name: Shared Asset ICPC",
                        "validation: custom",
                    ]
                )
                + "\n",
            )
            zf.writestr("lang/data/secret/001.in", "1\n")
            zf.writestr("lang/submissions/accepted/ac.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/input_validators/validator.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/output_validator/checker.cpp", "int main(){return 0;}\n")
            zf.writestr("lang/statement-sections/english/legend.tex", "Legend EN\n")
            zf.writestr("lang/statement-sections/chinese/legend.tex", "Legend ZH\n")
            zf.writestr("lang/statement-sections/english/diagram.png", b"EN")
            zf.writestr("lang/statement-sections/chinese/diagram.png", b"ZH")
            zf.writestr("lang/statement-sections/english/shared.png", b"SAME")
            zf.writestr("lang/statement-sections/chinese/shared.png", b"SAME")

        service = ICPCPackageImportService()
        result = service.import_package(ws, "lang.zip", payload.getvalue())
        self.assertEqual((ws / "statement-assets" / "diagram.png").read_bytes(), b"EN")
        self.assertEqual((ws / "statement-assets" / "diagram-zh.png").read_bytes(), b"ZH")
        self.assertEqual((ws / "statement-assets" / "shared.png").read_bytes(), b"SAME")
        self.assertFalse((ws / "statement-assets" / "shared-zh.png").exists())
        warnings = [str(item) for item in (result.get("warnings") or [])]
        self.assertTrue(any("statement-sections/chinese/diagram.png" in item for item in warnings))
        self.assertTrue(any("statement-assets/diagram-zh.png" in item for item in warnings))
        self.assertFalse(any("shared.png" in item for item in warnings))
