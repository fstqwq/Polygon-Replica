from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import yaml

from app.service.export.icpc_package import SUBMISSION_RULES
from app.service.importing.icpc import ICPCPackageImportService
from app.service.statement.title import (
    PROBLEM_TITLE_MAX_LEN,
    normalize_problem_title,
    statement_title_from_snapshot,
)


class TestICPCPackageImport(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="icpc-import-")
        self.workspace = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _workspace_path(self) -> Path:
        return self.workspace

    def _import_title_metadata(
        self,
        *,
        yaml_name: str = "",
        ini_name: str = "",
        external_id: str = "two-sum",
        package_name: str = "package.zip",
    ) -> dict[str, object]:
        payload = io.BytesIO()
        yaml_lines = ["validation: default"]
        if yaml_name:
            yaml_lines.insert(0, f"name: {yaml_name}")
        ini_lines = [f"externalid = {external_id}", "short-name = A"]
        if ini_name:
            ini_lines.insert(0, f"name = {ini_name}")
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem/problem.yaml", "\n".join(yaml_lines) + "\n")
            zf.writestr(
                "problem/domjudge-problem.ini",
                "\n".join(ini_lines) + "\n",
            )
            zf.writestr("problem/data/secret/001.in", "1\n")
        return ICPCPackageImportService().import_package(
            self.workspace,
            package_name,
            payload.getvalue(),
        )

    def test_import_title_prefers_yaml_name(self) -> None:
        result = self._import_title_metadata(
            yaml_name="'YAML # = O''Brien \u4e2d\u6587'",
            ini_name='"INI # = \\"\u4e2d\u6587\\""',
        )
        self.assertEqual(result["title"], "YAML # = O'Brien \u4e2d\u6587")
        self.assertEqual(
            (
                self.workspace
                / "statement-sections"
                / "english"
                / "name.tex"
            ).read_text(encoding="utf-8"),
            "YAML # = O'Brien \u4e2d\u6587\n",
        )

    def test_import_title_uses_domjudge_ini_name(self) -> None:
        result = self._import_title_metadata(
            ini_name='"INI # = \\"\u4e2d\u6587\\""',
        )
        self.assertEqual(result["title"], 'INI # = "\u4e2d\u6587"')

    def test_import_title_falls_back_to_public_slug(self) -> None:
        result = self._import_title_metadata(external_id="owner/two-sum")
        self.assertEqual(result["title"], "two-sum")

    def test_snapshot_title_uses_default_language_and_slug_fallback(self) -> None:
        english = self.workspace / "statement-sections" / "english"
        chinese = self.workspace / "statement-sections" / "chinese"
        english.mkdir(parents=True)
        chinese.mkdir(parents=True)
        (english / "name.tex").write_text("\n", encoding="utf-8")
        (chinese / "name.tex").write_text("Chinese Title\n", encoding="utf-8")
        self.assertEqual(
            statement_title_from_snapshot(
                self.workspace,
                fallback_title="two-sum",
            ),
            "two-sum",
        )
        (english / "name.tex").unlink()
        english.rmdir()
        self.assertEqual(
            statement_title_from_snapshot(
                self.workspace,
                fallback_title="two-sum",
            ),
            "Chinese Title",
        )

    def test_problem_title_boundary_rejects_multiline_and_oversized_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "single line"):
            normalize_problem_title("line one\nline two", fallback_title="two-sum")
        with self.assertRaisesRegex(ValueError, "too long"):
            normalize_problem_title(
                "x" * (PROBLEM_TITLE_MAX_LEN + 1),
                fallback_title="two-sum",
            )

    def test_problem_yaml_2025_metadata_supports_names_types_and_limits(self) -> None:
        metadata = ICPCPackageImportService()._parse_problem_yaml(  # pylint: disable=protected-access
            """
problem_format_version: 2025-09
name:
  zh: 中文题目
  en: "Quoted: # title"
type: [interactive, multi-pass]
limits:
  time_limit: 2.25
  memory: 1
  validation_passes: 3
"""
        )
        self.assertEqual(metadata["format_version"], "2025-09")
        self.assertEqual(metadata["title"], "Quoted: # title")
        self.assertEqual(metadata["mode"], "interactive")
        self.assertEqual(metadata["pass_limit"], 3)
        self.assertEqual(metadata["time_limit_ms"], 2250)
        self.assertEqual(metadata["memory_limit_mb"], 1)

    def test_problem_yaml_legacy_type_and_validation_remain_supported(self) -> None:
        service = ICPCPackageImportService()
        legacy_type = service._parse_problem_yaml(  # pylint: disable=protected-access
            """
problem_format_version: legacy
name: Legacy combined
type: interactive multi-pass
limits:
  validation_passes: 2
"""
        )
        self.assertEqual(legacy_type["mode"], "interactive")
        self.assertEqual(legacy_type["pass_limit"], 2)

        legacy_validation = service._parse_problem_yaml(  # pylint: disable=protected-access
            "name: Legacy validation\nvalidation: custom interactive\n"
        )
        self.assertEqual(legacy_validation["mode"], "interactive")
        self.assertEqual(legacy_validation["pass_limit"], 1)

    def test_problem_yaml_rejects_unknown_version_and_type(self) -> None:
        service = ICPCPackageImportService()
        with self.assertRaisesRegex(ValueError, "unsupported problem_format_version"):
            service._parse_problem_yaml(  # pylint: disable=protected-access
                "problem_format_version: 2099-01\nname: Future\n"
            )
        with self.assertRaisesRegex(ValueError, "unsupported ICPC problem type"):
            service._parse_problem_yaml(  # pylint: disable=protected-access
                "problem_format_version: 2025-09\nname: Scored\ntype: scoring\n"
            )

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
                        "limits:",
                        "  memory: 1",
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
        self.assertEqual(int(problem_cfg.get("memory_limit_mb") or 0), 1)

        build_cfg = json.loads((ws / "config" / "build.json").read_text(encoding="utf-8"))
        self.assertTrue(str(build_cfg.get("accepted_solution_source") or "").startswith("solutions/"))
        self.assertEqual(str(build_cfg.get("validator_source") or ""), "validators/validator.cpp")
        self.assertEqual(str(build_cfg.get("checker_source") or ""), "checkers/checker.cpp")

    def test_submission_yaml_round_trips_all_expected_behaviors_and_strips_annotations(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        metadata: dict[str, dict[str, object]] = {}
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "roundtrip/problem.yaml",
                "problem_format_version: 2025-09\nname: Submission Roundtrip\ntype: pass-fail\n",
            )
            zf.writestr("roundtrip/data/secret/001.in", "1\n")
            for expected, rule in SUBMISSION_RULES.items():
                rel = f"{rule['directory']}/{expected}.cpp"
                source = f"int {expected.replace('-', '_')}() {{ return 0; }}\n"
                if expected in {"tle_or_correct", "tle_or_re", "rejected"}:
                    results = ",".join(rule["domjudge_results"])
                    source = f"// @EXPECTED_RESULTS@: {results}\n{source}"
                zf.writestr(f"roundtrip/submissions/{rel}", source)
                metadata[rel] = {
                    "language": "cpp",
                    "permitted": list(rule["permitted"]),
                    "required": list(rule["required"]),
                }
            zf.writestr(
                "roundtrip/submissions/submissions.yaml",
                yaml.safe_dump(metadata, sort_keys=False),
            )

        result = ICPCPackageImportService().import_package(
            ws,
            "roundtrip.zip",
            payload.getvalue(),
        )

        self.assertFalse(result["warnings"])
        for expected in SUBMISSION_RULES:
            source = ws / "solutions" / f"{expected}.cpp"
            desc = ws / "solutions" / f"{expected}.cpp.desc"
            self.assertTrue(source.is_file())
            self.assertIn(f"expected: {expected}", desc.read_text(encoding="utf-8"))
            self.assertNotIn("@EXPECTED_RESULTS@", source.read_text(encoding="utf-8"))

    def test_submission_yaml_overrides_annotation_and_directory_with_warning(self) -> None:
        ws = self._workspace_path()
        payload = io.BytesIO()
        metadata = {
            "wrong_answer/conflict.cpp": {
                "language": "cpp",
                "permitted": ["AC"],
                "required": ["AC"],
            }
        }
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.yaml", "name: Conflict\nvalidation: default\n")
            zf.writestr("data/secret/001.in", "1\n")
            zf.writestr(
                "submissions/wrong_answer/conflict.cpp",
                "// @EXPECTED_RESULTS@: CORRECT,TIMELIMIT\nint main() { return 0; }\n",
            )
            zf.writestr("submissions/submissions.yaml", yaml.safe_dump(metadata))

        result = ICPCPackageImportService().import_package(
            ws,
            "conflict.zip",
            payload.getvalue(),
        )

        desc = (ws / "solutions" / "conflict.cpp.desc").read_text(encoding="utf-8")
        self.assertIn("expected: accepted", desc)
        source = (ws / "solutions" / "conflict.cpp").read_text(encoding="utf-8")
        self.assertNotIn("@EXPECTED_RESULTS@", source)
        warnings = [str(item) for item in result["warnings"]]
        self.assertTrue(any("overrides conflicting annotation" in item for item in warnings))
        self.assertTrue(any("overrides conflicting directory" in item for item in warnings))

    def test_submission_yaml_rejects_invalid_verdict(self) -> None:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.yaml", "name: Invalid\nvalidation: default\n")
            zf.writestr("data/secret/001.in", "1\n")
            zf.writestr("submissions/accepted/ac.cpp", "int main() { return 0; }\n")
            zf.writestr(
                "submissions/submissions.yaml",
                "accepted/ac.cpp:\n  permitted: [AC, CE]\n  required: [AC]\n",
            )

        with self.assertRaisesRegex(ValueError, "invalid permitted verdict"):
            ICPCPackageImportService().import_package(
                self.workspace,
                "invalid.zip",
                payload.getvalue(),
            )

    def test_non_first_line_expected_results_comment_is_preserved(self) -> None:
        payload = io.BytesIO()
        source = (
            "int main() { return 0; }\n"
            "// @EXPECTED_RESULTS@: CORRECT,TIMELIMIT\n"
        )
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("problem.yaml", "name: User Comment\nvalidation: default\n")
            zf.writestr("data/secret/001.in", "1\n")
            zf.writestr("submissions/accepted/ac.cpp", source)

        ICPCPackageImportService().import_package(
            self.workspace,
            "user-comment.zip",
            payload.getvalue(),
        )

        self.assertEqual(
            (self.workspace / "solutions" / "ac.cpp").read_text(encoding="utf-8"),
            source,
        )

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
