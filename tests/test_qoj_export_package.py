import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.config import ConfigValues
from app.service.export.adapters import PackageAdapterRegistry
from app.service.export.adapters.qoj import QOJPackageAdapter
from app.service.problem.runtime_config import ProblemMode
from app.service.problem_package.manifest import (
    NativePackageManifest,
    NativePackageTestEntry,
    describe_file,
)
from app.service.problem_package.service import NativePackageReader
from app.service.problem_package.store import MaterializationRow


class TestQOJExportPackage(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="qoj-export-package-"
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
            },
            normalizer=lambda raw: raw,
        )
        self.tex_compile = mock.Mock()

        def compile_pdf(entrypoint: Path) -> SimpleNamespace:
            pdf = entrypoint.with_suffix(".pdf")
            pdf.write_bytes(
                b"%PDF-1.4\n" + entrypoint.parent.name.encode("utf-8") + b"\n"
            )
            return SimpleNamespace(
                proc=SimpleNamespace(returncode=0, stderr="", stdout=""),
                pdf_path=pdf,
            )

        self.tex_compile.compile_pdf.side_effect = compile_pdf

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_pass_fail_package_uses_qoj_root_layout_and_sample_overrides(
        self,
    ) -> None:
        reader = self._reader(
            name="pass-fail",
            mode="pass-fail",
            pass_limit=1,
            checker="ncmp",
        )
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        target = self.root / "pass-fail-package"

        warning = adapter.build(
            reader,
            target=target,
            canonical_problem_slug="owner/problem",
        )

        self.assertEqual(warning, "")
        self.assertEqual(
            (target / "problem.conf").read_text(encoding="utf-8"),
            "use_builtin_judger on\n"
            "use_builtin_checker ncmp\n"
            "n_tests 2\n"
            "n_ex_tests 1\n"
            "n_sample_tests 1\n"
            "input_suf in\n"
            "output_suf ans\n"
            "n_subtasks 1\n"
            "subtask_end_1 2\n"
            "subtask_score_1 100\n"
            "time_limit 1.25\n"
            "memory_limit 512\n"
            "use_pdf_statement on\n",
        )
        self.assertEqual((target / "1.in").read_bytes(), b"judge input\n")
        self.assertEqual((target / "1.ans").read_bytes(), b"judge answer\n")
        self.assertEqual((target / "2.in").read_bytes(), b"secret input\n")
        self.assertEqual((target / "2.ans").read_bytes(), b"secret answer\n")
        self.assertEqual((target / "ex_1.in").read_bytes(), b"display input\n")
        self.assertEqual(
            (target / "ex_1.ans").read_bytes(),
            b"display answer\n",
        )
        self.assertEqual(
            (target / "std.cpp").read_text(encoding="utf-8"),
            "accepted source\n",
        )
        self.assertEqual(
            (target / "val.cpp").read_text(encoding="utf-8"),
            "int main() {}\n",
        )
        self.assertFalse((target / "chk.cpp").exists())
        self.assertEqual(
            (target / "download" / "tools" / "helper.txt").read_text(
                encoding="utf-8"
            ),
            "helper\n",
        )
        self.assertFalse((target / "attachments").exists())
        self.assertFalse((target / "download.zip").exists())
        self.assertEqual(
            (target / "statement.pdf").read_bytes(),
            b"%PDF-1.4\nenglish\n",
        )
        compiled = self.tex_compile.compile_pdf.call_args.args[0]
        self.assertEqual(
            compiled.relative_to(reader.root).as_posix(),
            "statement-build/english/statements.tex",
        )

    def test_standard_custom_and_missing_checkers_have_distinct_adapter_output(
        self,
    ) -> None:
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        cases = (
            ("fcmp", "fcmp", False),
            ("ncmp", "ncmp", False),
            ("wcmp", "wcmp", False),
            ("custom", None, True),
            (None, None, True),
        )
        for checker, expected_builtin, expects_source in cases:
            with self.subTest(checker=checker):
                name = checker or "missing"
                reader = self._reader(
                    name=f"checker-{name}",
                    mode="pass-fail",
                    pass_limit=1,
                    checker=checker,
                )
                target = self.root / f"checker-package-{name}"
                adapter.build(
                    reader,
                    target=target,
                    canonical_problem_slug="owner/problem",
                )
                conf = (target / "problem.conf").read_text(encoding="utf-8")
                if expected_builtin is None:
                    self.assertNotIn("use_builtin_checker", conf)
                else:
                    self.assertIn(
                        f"use_builtin_checker {expected_builtin}\n",
                        conf,
                    )
                self.assertEqual((target / "chk.cpp").exists(), expects_source)
                if checker == "custom":
                    self.assertEqual(
                        (target / "chk.cpp").read_text(encoding="utf-8"),
                        "int custom_checker;\n",
                    )
                if checker is None:
                    exact_source = (target / "chk.cpp").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("std::ios::binary", exact_source)
                    self.assertIn("output differs from answer", exact_source)

    def test_interactive_packages_use_qoj_modes_and_empty_answers(self) -> None:
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        for pass_limit in (1, 2):
            with self.subTest(pass_limit=pass_limit):
                reader = self._reader(
                    name=f"interactive-{pass_limit}",
                    mode="interactive",
                    pass_limit=pass_limit,
                    checker=None,
                    include_answers=False,
                )
                target = self.root / f"interactive-package-{pass_limit}"

                adapter.build(
                    reader,
                    target=target,
                    canonical_problem_slug="owner/problem",
                )

                conf = (target / "problem.conf").read_text(encoding="utf-8")
                for line in (
                    "use_builtin_checker irscmp",
                    "interaction_mode on",
                ):
                    self.assertIn(f"{line}\n", conf)
                if pass_limit == 2:
                    for line in (
                        "polygon_runtwice on",
                        "polygon_runtwice_interactive on",
                        "interactor_run_type default",
                    ):
                        self.assertIn(f"{line}\n", conf)
                else:
                    self.assertNotIn("polygon_runtwice", conf)
                self.assertEqual((target / "1.ans").read_bytes(), b"")
                self.assertEqual((target / "2.ans").read_bytes(), b"")
                self.assertEqual((target / "ex_1.ans").read_bytes(), b"")
                self.assertTrue((target / "interactor.cpp").is_file())
                self.assertFalse((target / "chk.cpp").exists())

    def test_two_pass_pass_fail_package_uses_polygon_runtwice(self) -> None:
        reader = self._reader(
            name="two-pass-pass-fail",
            mode="pass-fail",
            pass_limit=2,
            checker="ncmp",
        )
        target = self.root / "two-pass-pass-fail-package"

        QOJPackageAdapter(self.values, self.tex_compile).build(
            reader,
            target=target,
            canonical_problem_slug="owner/problem",
        )

        conf = (target / "problem.conf").read_text(encoding="utf-8")
        self.assertIn("polygon_runtwice on\n", conf)
        self.assertNotIn("interaction_mode", conf)
        self.assertNotIn("polygon_runtwice_interactive", conf)

    def test_plan_warns_when_hack_sources_are_incomplete(self) -> None:
        reader = self._reader(
            name="hack-disabled",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
            include_accepted=False,
            include_validator=False,
        )
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        target = self.root / "hack-disabled-package"

        self.assertEqual(
            adapter.plan(reader).warning,
            "QOJ Hack must be disabled until std and val are available.",
        )
        self.assertEqual(
            adapter.build(
                reader,
                target=target,
                canonical_problem_slug="owner/problem",
            ),
            "QOJ Hack must be disabled until std and val are available.",
        )
        self.assertFalse((target / "std.cpp").exists())
        self.assertFalse((target / "val.cpp").exists())

    def test_accepted_solution_preserves_supported_source_extension(self) -> None:
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        for suffix, qoj_suffix in (
            (".c++", ".cpp"),
            (".cc", ".cpp"),
            (".cpp", ".cpp"),
            (".cxx", ".cpp"),
            (".java", ".java"),
            (".py", ".py"),
        ):
            with self.subTest(suffix=suffix, qoj_suffix=qoj_suffix):
                reader = self._reader(
                    name=f"accepted-{suffix[1:]}",
                    mode="pass-fail",
                    pass_limit=1,
                    checker=None,
                    accepted_suffix=suffix,
                )
                target = self.root / f"accepted-package-{suffix[1:]}"
                adapter.build(
                    reader,
                    target=target,
                    canonical_problem_slug="owner/problem",
                )

                self.assertEqual(
                    (target / f"std{qoj_suffix}").read_text(encoding="utf-8"),
                    "accepted source\n",
                )

    def test_statement_prefers_english_then_first_available_language(self) -> None:
        reader = self._reader(
            name="chinese-statement",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
        )
        shutil.rmtree(reader.root / "statement-sections" / "english")
        shutil.rmtree(reader.root / "statement-build" / "english")
        target = self.root / "chinese-statement-package"

        QOJPackageAdapter(self.values, self.tex_compile).build(
            reader,
            target=target,
            canonical_problem_slug="owner/problem",
        )

        self.assertEqual(
            (target / "statement.pdf").read_bytes(),
            b"%PDF-1.4\nchinese\n",
        )

    def test_rejects_unsupported_passes_memory_and_missing_statement(self) -> None:
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        unsupported_passes = self._reader(
            name="three-pass",
            mode="pass-fail",
            pass_limit=3,
            checker=None,
        )
        with self.assertRaisesRegex(ValueError, "at most two passes"):
            adapter.plan(unsupported_passes)

        excess_memory = self._reader(
            name="large-memory",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
            memory_limit_mb=6145,
        )
        with self.assertRaisesRegex(ValueError, "exceeds 6144 MiB"):
            adapter.plan(excess_memory)

        without_statement = self._reader(
            name="without-statement",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
            include_statement=False,
        )
        with self.assertRaisesRegex(ValueError, "problem statement"):
            adapter.build(
                without_statement,
                target=self.root / "without-statement-package",
                canonical_problem_slug="owner/problem",
            )

    def test_rejects_missing_and_escaping_native_test_payloads(self) -> None:
        adapter = QOJPackageAdapter(self.values, self.tex_compile)
        missing = self._reader(
            name="missing-input",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
        )
        first_test = missing.manifest["tests"][0]
        input_path = missing.payload(first_test, "input")
        self.assertIsNotNone(input_path)
        input_path.unlink()
        with self.assertRaisesRegex(ValueError, "input artifact is missing"):
            adapter.build(
                missing,
                target=self.root / "missing-input-package",
                canonical_problem_slug="owner/problem",
            )

        escaping = self._reader(
            name="escaping-input",
            mode="pass-fail",
            pass_limit=1,
            checker=None,
        )
        outside = self.root / "outside-input"
        outside.write_bytes(b"outside\n")
        escaping.manifest["tests"][0]["input"] = {
            "path": "../outside-input",
            "sha256": "0" * 64,
            "size": outside.stat().st_size,
        }
        with self.assertRaisesRegex(ValueError, "escapes the package"):
            adapter.build(
                escaping,
                target=self.root / "escaping-input-package",
                canonical_problem_slug="owner/problem",
            )

    def test_registry_places_qoj_before_nowcoder(self) -> None:
        registry = PackageAdapterRegistry(self.values, self.tex_compile)

        self.assertEqual(
            registry.formats,
            (
                "domjudge",
                "icpc-2025-09",
                "qoj",
                "polygon-linux",
                "nowcoder",
            ),
        )
        self.assertEqual(registry.require("qoj").display_name, "QOJ")

    def _reader(
        self,
        *,
        name: str,
        mode: ProblemMode,
        pass_limit: int,
        checker: str | None,
        include_answers: bool = True,
        include_accepted: bool = True,
        include_validator: bool = True,
        include_statement: bool = True,
        memory_limit_mb: int = 512,
        accepted_suffix: str = ".cpp",
    ) -> NativePackageReader:
        package_root = self.root / name
        (package_root / "config").mkdir(parents=True)
        (package_root / "tests").mkdir()
        (package_root / "solutions").mkdir()
        (package_root / "attachments" / "tools").mkdir(parents=True)
        (package_root / "attachments" / "tools" / "helper.txt").write_text(
            "helper\n",
            encoding="utf-8",
        )
        (package_root / "config" / "problem.json").write_text(
            json.dumps(
                {
                    "time_limit_ms": 1250,
                    "memory_limit_mb": memory_limit_mb,
                    "mode": mode,
                    "pass_limit": pass_limit,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        build_config: dict[str, object] = {"generator_sources": []}
        if include_accepted:
            accepted = package_root / "solutions" / f"main{accepted_suffix}"
            accepted.write_text("accepted source\n", encoding="utf-8")
            build_config["accepted_solution_source"] = (
                f"solutions/main{accepted_suffix}"
            )
        if include_validator:
            validator = package_root / "validators" / "validator.cc"
            validator.parent.mkdir()
            validator.write_text("int main() {}\n", encoding="utf-8")
            build_config["validator_source"] = "validators/validator.cc"
        if mode == "interactive":
            interactor = package_root / "interactors" / "main.cc"
            interactor.parent.mkdir()
            interactor.write_text("int main() {}\n", encoding="utf-8")
            build_config["interactor_source"] = "interactors/main.cc"
        elif checker is not None:
            checker_source = package_root / "checkers" / "selected.cpp"
            checker_source.parent.mkdir()
            if checker == "custom":
                checker_source.write_text(
                    "int custom_checker;\n",
                    encoding="utf-8",
                )
            else:
                vendored = (
                    Path(__file__).resolve().parents[1]
                    / "third_party"
                    / "testlib"
                    / "checkers"
                    / f"{checker}.cpp"
                )
                shutil.copy2(vendored, checker_source)
            build_config["checker_source"] = "checkers/selected.cpp"
        (package_root / "config" / "build.json").write_text(
            json.dumps(build_config) + "\n",
            encoding="utf-8",
        )
        (package_root / "tests" / "spec.json").write_text(
            json.dumps(
                {
                    "tests": [
                        {"id": "001", "kind": "manual", "sample": True},
                        {"id": "custom", "kind": "manual", "sample": False},
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        if include_statement:
            for language in ("english", "chinese"):
                (package_root / "statement-sections" / language).mkdir(
                    parents=True
                )
                statement_build = package_root / "statement-build" / language
                statement_build.mkdir(parents=True)
                (statement_build / "statements.tex").write_text(
                    "statement\n",
                    encoding="utf-8",
                )

        tests: list[NativePackageTestEntry] = []
        test_payloads = (
            (
                "001",
                b"judge input\n",
                b"judge answer\n",
                b"display input\n",
                b"display answer\n",
                True,
            ),
            (
                "custom",
                b"secret input\n",
                b"secret answer\n",
                None,
                None,
                False,
            ),
        )
        for (
            test_id,
            input_bytes,
            answer_bytes,
            sample_input,
            sample_output,
            sample,
        ) in test_payloads:
            test_root = package_root / "test-data" / "tests" / test_id
            test_root.mkdir(parents=True)
            input_path = test_root / "input"
            input_path.write_bytes(input_bytes)
            entry: NativePackageTestEntry = {
                "id": test_id,
                "kind": "manual",
                "sample": sample,
                "input": describe_file(input_path, root=package_root),
            }
            if include_answers:
                answer_path = test_root / "answer"
                answer_path.write_bytes(answer_bytes)
                entry["answer"] = describe_file(answer_path, root=package_root)
            if sample_input is not None:
                sample_input_path = test_root / "sample-input"
                sample_input_path.write_bytes(sample_input)
                entry["sample_input"] = describe_file(
                    sample_input_path,
                    root=package_root,
                )
            if sample_output is not None and include_answers:
                sample_output_path = test_root / "sample-output"
                sample_output_path.write_bytes(sample_output)
                entry["sample_output"] = describe_file(
                    sample_output_path,
                    root=package_root,
                )
            tests.append(entry)

        materialization: MaterializationRow = {
            "id": f"pm-{name}",
            "problem_id": 1,
            "source_commit": "a" * 40,
            "revision_number": 1,
            "source_digest": "b" * 64,
            "archive_rel_path": "materializations/verified.zip",
            "archive_sha256": "c" * 64,
            "archive_size_bytes": 1,
            "verification_id": "ver-qoj",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "checked_at": "2026-01-01T00:00:00Z",
            "unavailable_reason": "",
        }
        manifest: NativePackageManifest = {
            "source_commit": materialization["source_commit"],
            "revision_number": materialization["revision_number"],
            "source_digest": materialization["source_digest"],
            "mode": mode,
            "pass_limit": pass_limit,
            "solutions": [],
            "tests": tests,
        }
        return NativePackageReader(
            native_package=materialization,
            root=package_root,
            manifest=manifest,
        )


if __name__ == "__main__":
    unittest.main()
