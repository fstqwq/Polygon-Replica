
import json
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import yaml

from app.config import ConfigValues
from app.service.export.adapters.shared import (
    SUBMISSION_RULES,
    annotated_submission,
    write_input_validator,
    write_output_validator,
)
from app.service.export.adapters.domjudge import render_domjudge_problem_yaml
from app.service.export.adapters.icpc_2025 import (
    problem_uuid,
    render_problem_yaml,
    render_submissions_yaml,
)
from app.service.export.adapters import PackageAdapterRegistry
from app.service.problem_package.manifest import NativePackageManifest, describe_file
from app.service.problem_package.service import NativePackageReader
from app.service.problem_package.store import MaterializationRow


class TestICPCExportPackage(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="icpc-export-package-")
        self.root = Path(self._temp_dir.name)
        self.snapshot = self.root / "snapshot"
        self.package = self.root / "package"
        (self.snapshot / "third_party" / "testlib").mkdir(parents=True)
        (self.snapshot / "third_party" / "testlib" / "testlib.h").write_text(
            "// test header\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_problem_yaml_uses_safe_unicode_and_deterministic_identity(self) -> None:
        slug = "owner/problem #1 + 中文"
        text = render_problem_yaml(
            problem_slug=slug,
            source_commit="a" * 40,
            names={"en": "O'Brien: #1", "zh": "中文题目"},
            mode="pass-fail",
            pass_limit=1,
            time_limit_ms=2250,
            memory_limit_mb=1,
        )
        metadata = yaml.safe_load(text)
        self.assertEqual(metadata["problem_format_version"], "2025-09")
        self.assertEqual(metadata["type"], "pass-fail")
        self.assertEqual(
            metadata["name"],
            {"en": "O'Brien: #1", "zh": "中文题目"},
        )
        self.assertEqual(
            metadata["uuid"],
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"polygon-replica/problem/{slug}")),
        )
        self.assertEqual(problem_uuid(slug), metadata["uuid"])
        self.assertEqual(metadata["version"], "a" * 40)
        self.assertEqual(metadata["limits"], {"time_limit": 2.25, "memory": 1})

    def test_problem_yaml_uses_all_2025_09_execution_types(self) -> None:
        cases = (
            ("pass-fail", 1, "pass-fail"),
            ("interactive", 1, ["pass-fail", "interactive"]),
            ("pass-fail", 3, ["pass-fail", "multi-pass"]),
            (
                "interactive",
                3,
                ["pass-fail", "interactive", "multi-pass"],
            ),
        )
        for mode, pass_limit, expected_type in cases:
            with self.subTest(mode=mode, pass_limit=pass_limit):
                metadata = yaml.safe_load(
                    render_problem_yaml(
                        problem_slug="owner/problem",
                        source_commit="b" * 40,
                        names={"en": "Problem"},
                        mode=mode,
                        pass_limit=pass_limit,
                        time_limit_ms=1000,
                        memory_limit_mb=None,
                    )
                )
                self.assertEqual(metadata["problem_format_version"], "2025-09")
                self.assertEqual(metadata["type"], expected_type)
                if pass_limit > 1:
                    self.assertEqual(metadata["limits"]["validation_passes"], pass_limit)
                else:
                    self.assertNotIn("validation_passes", metadata["limits"])

    def test_domjudge_problem_yaml_uses_legacy_validation_fields(self) -> None:
        cases = (
            ("pass-fail", 1, "custom", "pass-fail"),
            ("interactive", 1, "custom interactive", "pass-fail"),
            ("pass-fail", 3, "custom multi-pass", "pass-fail"),
            (
                "interactive",
                3,
                "custom interactive",
                "pass-fail multi-pass",
            ),
        )
        for mode, pass_limit, expected_validation, expected_type in cases:
            with self.subTest(mode=mode, pass_limit=pass_limit):
                metadata = yaml.safe_load(
                    render_domjudge_problem_yaml(
                        names={"en": "Problem", "zh": "题目"},
                        mode=mode,
                        pass_limit=pass_limit,
                        time_limit_ms=1000,
                        memory_limit_mb=256,
                    )
                )
                self.assertEqual(metadata["problem_format_version"], "legacy")
                self.assertEqual(metadata["type"], expected_type)
                self.assertEqual(metadata["validation"], expected_validation)
                self.assertEqual(metadata["name"], "Problem")

    def test_submissions_yaml_preserves_all_expected_result_sets(self) -> None:
        entries = {
            f"{rule['ppf_directory']}/{expected}.cpp": {
                "language": "cpp",
                "permitted": list(rule["permitted"]),
                "required": list(rule["required"]),
            }
            for expected, rule in SUBMISSION_RULES.items()
        }
        self.assertEqual(yaml.safe_load(render_submissions_yaml(entries)), entries)

    def test_mixed_annotation_is_language_legal_and_does_not_mutate_source(self) -> None:
        source = self.snapshot / "mixed.py"
        original = "print('ok')\n"
        source.write_text(original, encoding="utf-8")
        payload = annotated_submission(source, ("CORRECT", "TIMELIMIT"))
        self.assertTrue(payload.startswith(b"# @EXPECTED_RESULTS@: CORRECT,TIMELIMIT\n"))
        self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_domjudge_submission_rules_separate_standard_and_mixed_results(self) -> None:
        standard = {
            "accepted": ("accepted", ("CORRECT",)),
            "wrong_answer": (
                "wrong_answer",
                ("CORRECT", "WRONG-ANSWER"),
            ),
            "time_limit_exceeded": (
                "time_limit_exceeded",
                ("CORRECT", "TIMELIMIT"),
            ),
            "run_time_error": (
                "run_time_error",
                ("CORRECT", "RUN-ERROR"),
            ),
        }
        mixed = {
            "tle_or_correct": ("CORRECT", "TIMELIMIT"),
            "tle_or_re": ("TIMELIMIT", "RUN-ERROR"),
            "rejected": (
                "WRONG-ANSWER",
                "TIMELIMIT",
                "RUN-ERROR",
                "COMPILER-ERROR",
            ),
        }
        for behavior, (directory, results) in standard.items():
            with self.subTest(behavior=behavior):
                rule = SUBMISSION_RULES[behavior]
                self.assertEqual(rule["domjudge_directory"], directory)
                self.assertEqual(rule["domjudge_results"], results)
        for behavior, results in mixed.items():
            with self.subTest(behavior=behavior):
                rule = SUBMISSION_RULES[behavior]
                self.assertEqual(rule["domjudge_directory"], "mixed")
                self.assertEqual(rule["domjudge_results"], results)

    def test_validator_wrappers_map_exit_codes_and_preserve_output(self) -> None:
        validator = self.snapshot / "validator.py"
        validator.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "data = sys.stdin.read().strip()\n"
            "if len(sys.argv) >= 4:\n"
            "    Path(sys.argv[3], 'nextpass.in').write_text('next\\n')\n"
            "print('validator-output')\n"
            "raise SystemExit(int(data or '0'))\n",
            encoding="utf-8",
        )
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=validator)
        write_output_validator(snapshot=self.snapshot, package_root=self.package, source=validator)

        input_run = self.package / "input_validators" / "validator" / "run"
        accepted = subprocess.run(
            [str(input_run)], input="0\n", text=True, capture_output=True, check=False
        )
        self.assertEqual(accepted.returncode, 42)
        rejected = subprocess.run(
            [str(input_run)], input="7\n", text=True, capture_output=True, check=False
        )
        self.assertEqual(rejected.returncode, 7)

        feedback = self.root / "feedback"
        feedback.mkdir()
        output_run = self.package / "output_validator" / "run"
        wrong = subprocess.run(
            [str(output_run), "input", "answer", str(feedback)],
            input="1\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(wrong.returncode, 43)
        self.assertEqual(wrong.stdout, "validator-output\n")
        self.assertEqual((feedback / "nextpass.in").read_text(encoding="utf-8"), "next\n")
        judge_error = subprocess.run(
            [str(output_run), "input", "answer", str(feedback)],
            input="7\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(judge_error.returncode, 7)

    def test_cpp_validator_build_script_builds_and_maps_success(self) -> None:
        self.assertIsNotNone(shutil.which("c++"), "Linux test host must provide c++")
        validator = self.snapshot / "validator name.cpp"
        validator.write_text("int main() { return 0; }\n", encoding="utf-8")
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=validator)
        validator_dir = self.package / "input_validators" / "validator"
        subprocess.run([str(validator_dir / "build")], cwd=validator_dir, check=True)
        result = subprocess.run([str(validator_dir / "run")], cwd=validator_dir, check=False)
        self.assertEqual(result.returncode, 42)

    def test_c_validator_source_is_rejected(self) -> None:
        validator = self.snapshot / "validator.c"
        validator.write_text("int main(void) { return 0; }\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ValueError,
            r"unsupported ICPC validator language: \.c",
        ):
            write_output_validator(
                snapshot=self.snapshot,
                package_root=self.package,
                source=validator,
            )
        self.assertFalse((self.package / "output_validator" / "validator.c").exists())

    def test_missing_input_validator_gets_explicit_accept_all_program(self) -> None:
        write_input_validator(snapshot=self.snapshot, package_root=self.package, source=None)
        run = self.package / "input_validators" / "accept_all" / "run"
        result = subprocess.run(
            [str(run)], input=b"arbitrary input\n", capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 42)

    def test_adapters_publish_disjoint_strict_and_domjudge_layouts(self) -> None:
        reader = self._adapter_reader(mode="interactive", pass_limit=2)
        values = ConfigValues(
            {
                "GENERAL_TIME_LIMIT_MIN_MS": 1,
                "GENERAL_TIME_LIMIT_MAX_MS": 60_000,
                "GENERAL_MEMORY_LIMIT_MIN_MB": 1,
                "GENERAL_MEMORY_LIMIT_MAX_MB": 2048,
                "GENERAL_PASS_LIMIT_MIN": 1,
                "GENERAL_PASS_LIMIT_MAX": 32,
                "TEXTAREA_MAX_BYTES": 1024 * 1024,
                "STATEMENT_SAMPLE_MAX_BYTES": 1024 * 1024,
            },
            normalizer=lambda raw: raw,
        )
        adapters = PackageAdapterRegistry(values, mock.Mock())

        def write_statements(
            _snapshot: Path,
            destination: Path,
            *,
            problem_name: str,
            include_sample_tests: bool,
            keep_all_languages: bool,
        ) -> dict[str, str]:
            self.assertEqual(problem_name, "projected-problem")
            self.assertFalse(include_sample_tests)
            destination.mkdir(parents=True)
            filename = "problem.en.pdf" if keep_all_languages else "problem.pdf"
            (destination / filename).write_bytes(b"%PDF-1.4\n")
            return {"en": problem_name}

        strict = self.root / "strict"
        domjudge = self.root / "domjudge"
        self.assertEqual(
            adapters.formats,
            ("domjudge", "icpc-2025-09", "qoj", "nowcoder"),
        )
        domjudge_adapter = adapters.require("domjudge")
        strict_adapter = adapters.require("icpc-2025-09")
        with (
            mock.patch.object(
                domjudge_adapter,
                "write_statements",
                side_effect=write_statements,
            ),
            mock.patch.object(
                strict_adapter,
                "write_statements",
                side_effect=write_statements,
            ),
        ):
            strict_warning = strict_adapter.build(
                reader,
                target=strict,
                canonical_problem_slug="owner/projected-problem",
            )
            domjudge_warning = domjudge_adapter.build(
                reader,
                target=domjudge,
                canonical_problem_slug="owner/projected-problem",
                short_name="A",
            )

        self.assertIn("solutions/mixed.cpp", strict_warning)
        self.assertEqual(domjudge_warning, "")

        self.assertTrue((strict / "statement" / "problem.en.pdf").is_file())
        self.assertTrue((strict / "submissions" / "submissions.yaml").is_file())
        self.assertFalse((strict / "domjudge-problem.ini").exists())
        self.assertFalse((strict / "problem_statement").exists())
        strict_metadata = yaml.safe_load((strict / "problem.yaml").read_text())
        self.assertEqual(strict_metadata["problem_format_version"], "2025-09")
        self.assertEqual(
            strict_metadata["type"],
            ["pass-fail", "interactive", "multi-pass"],
        )
        self.assertFalse((strict / "submissions" / "mixed_rejected" / "mixed.cpp").exists())
        self.assertEqual((strict / "data" / "secret" / "001.ans").read_bytes(), b"")

        self.assertTrue((domjudge / "problem_statement" / "problem.pdf").is_file())
        self.assertTrue((domjudge / "domjudge-problem.ini").is_file())
        self.assertFalse((domjudge / "statement").exists())
        self.assertFalse((domjudge / "submissions" / "submissions.yaml").exists())
        domjudge_metadata = yaml.safe_load((domjudge / "problem.yaml").read_text())
        self.assertEqual(domjudge_metadata["problem_format_version"], "legacy")
        self.assertEqual(domjudge_metadata["type"], "pass-fail multi-pass")
        self.assertEqual(
            domjudge_metadata["validation"],
            "custom interactive",
        )
        self.assertIn(
            "short-name = A\n",
            (domjudge / "domjudge-problem.ini").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "externalid = projected-problem\n",
            (domjudge / "domjudge-problem.ini").read_text(encoding="utf-8"),
        )
        domjudge_mixed = domjudge / "submissions" / "mixed" / "mixed.cpp"
        self.assertTrue(
            domjudge_mixed.read_bytes().startswith(
                b"// @EXPECTED_RESULTS@: WRONG-ANSWER,TIMELIMIT,RUN-ERROR,COMPILER-ERROR\n"
            )
        )
        for package_root in (strict, domjudge):
            with self.subTest(package_root=package_root.name):
                validator_dir = package_root / "output_validator"
                build = validator_dir / "build"
                run = validator_dir / "run"
                self.assertIn("-DDOMJUDGE", build.read_text(encoding="utf-8"))
                self.assertTrue(build.stat().st_mode & stat.S_IXUSR)
                self.assertTrue(run.stat().st_mode & stat.S_IXUSR)
                subprocess.run([str(build)], cwd=validator_dir, check=True)
                result = subprocess.run(
                    [str(run), "input", "answer", "feedback"],
                    cwd=validator_dir,
                    check=False,
                )
                self.assertEqual(result.returncode, 42)

    def _adapter_reader(
        self,
        *,
        mode: str,
        pass_limit: int,
    ) -> NativePackageReader:
        package_root = self.root / "verified"
        (package_root / "config").mkdir(parents=True)
        (package_root / "tests").mkdir()
        (package_root / "solutions").mkdir()
        (package_root / "attachments").mkdir()
        (package_root / "third_party" / "testlib").mkdir(parents=True)
        (package_root / "third_party" / "testlib" / "testlib.h").write_text(
            "// test header\n",
            encoding="utf-8",
        )
        (package_root / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "time_limit_ms": 1500,
                    "memory_limit_mb": 256,
                    "mode": mode,
                    "pass_limit": pass_limit,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        build_config = {
            "accepted_solution_source": "solutions/accepted.cpp",
            "generator_sources": [],
        }
        if mode == "interactive":
            (package_root / "interactors").mkdir()
            (package_root / "interactors" / "interactor.cpp").write_text(
                "#ifndef DOMJUDGE\n"
                "#error DOMJUDGE must be defined by the package build file\n"
                "#endif\n"
                "int main() {}\n",
                encoding="utf-8",
            )
            build_config["interactor_source"] = "interactors/interactor.cpp"
        (package_root / "config" / "build.json").write_text(
            json.dumps(build_config) + "\n",
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
                            "sample_input": "1\n",
                            "sample_output": "",
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (package_root / "solutions" / "accepted.cpp").write_text(
            "int main() {}\n",
            encoding="utf-8",
        )
        (package_root / "solutions" / "mixed.cpp").write_text(
            "int mixed;\n",
            encoding="utf-8",
        )
        (package_root / "solutions" / "mixed.cpp.desc").write_text(
            "expected: rejected\n",
            encoding="utf-8",
        )
        (package_root / "attachments" / "readme.txt").write_text(
            "attachment\n",
            encoding="utf-8",
        )
        test_root = package_root / "test-data" / "tests" / "001"
        test_root.mkdir(parents=True)
        input_path = test_root / "input"
        input_path.write_bytes(b"1\n")
        materialization: MaterializationRow = {
            "id": "pm-adapter",
            "problem_id": 1,
            "source_commit": "a" * 40,
            "revision_number": 3,
            "source_digest": "b" * 64,
            "archive_rel_path": "materializations/verified.zip",
            "archive_sha256": "c" * 64,
            "archive_size_bytes": 1,
            "verification_id": "ver-adapter",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "checked_at": "2026-01-01T00:00:00Z",
            "unavailable_reason": "",
        }
        manifest: NativePackageManifest = {
            "source_commit": materialization["source_commit"],
            "revision_number": 3,
            "source_digest": materialization["source_digest"],
            "mode": mode,
            "pass_limit": pass_limit,
            "verification": {"id": "ver-adapter", "source": "full"},
            "solutions": [
                {
                    "source_path": "solutions/accepted.cpp",
                    "expected_behavior": "accepted",
                    "verdicts": ["AC"],
                },
                {
                    "source_path": "solutions/mixed.cpp",
                    "expected_behavior": "rejected",
                    "verdicts": ["WA", "CE"],
                },
            ],
            "tests": [
                {
                    "id": "001",
                    "kind": "manual",
                    "sample": True,
                    "input": describe_file(input_path, root=package_root),
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
