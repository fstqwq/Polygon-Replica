"""QOJ test-data package adapter."""

import shutil
from pathlib import Path
from typing import cast

from app.service.export.adapters.shared import (
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem.build_config import BuildConfig
from app.service.problem.runtime_config import ProblemConfig
from app.service.problem.standard_checker import detect_standard_checker
from app.service.problem_package.layout import STATEMENT_BUILD_DIR
from app.service.problem_package.manifest import NativePackageTestEntry
from app.service.problem_package.service import NativePackageReader
from app.service.statement.context import statement_languages


QOJ_MEMORY_LIMIT_MB = 6144
_QOJ_BUILTIN_CHECKERS = {
    "fcmp.cpp": "fcmp",
    "ncmp.cpp": "ncmp",
    "wcmp.cpp": "wcmp",
}
_QOJ_SOLUTION_SUFFIXES = {
    ".c++": ".cpp",
    ".cc": ".cpp",
    ".cpp": ".cpp",
    ".cxx": ".cpp",
    ".java": ".java",
    ".py": ".py",
}
_CPP_SOURCE_SUFFIXES = frozenset({".c++", ".cc", ".cpp", ".cxx"})
_EXACT_OUTPUT_CHECKER = """#include \"testlib.h\"
#include <fstream>

int main(int argc, char* argv[]) {
    registerTestlibCmd(argc, argv);
    std::ifstream output(argv[2], std::ios::binary);
    std::ifstream answer(argv[3], std::ios::binary);
    if (!output || !answer) {
        quitf(_fail, \"cannot open output or answer\");
    }
    while (true) {
        const int actual = output.get();
        const int expected = answer.get();
        if (actual != expected) {
            quitf(_wa, \"output differs from answer\");
        }
        if (actual == std::char_traits<char>::eof()) {
            break;
        }
    }
    quitf(_ok, \"output matches answer\");
}
"""


def _time_limit_seconds(time_limit_ms: int) -> str:
    whole, milliseconds = divmod(time_limit_ms, 1000)
    if milliseconds == 0:
        return str(whole)
    return f"{whole}.{milliseconds:03d}".rstrip("0")


def render_qoj_problem_conf(
    *,
    problem_config: ProblemConfig,
    builtin_checker: str | None,
    test_count: int,
    sample_count: int,
) -> str:
    """Render the supported QOJ ``problem.conf`` subset."""

    lines = ["use_builtin_judger on"]
    if builtin_checker is not None:
        lines.append(f"use_builtin_checker {builtin_checker}")
    lines.extend(
        (
            f"n_tests {test_count}",
            f"n_ex_tests {sample_count}",
            f"n_sample_tests {sample_count}",
            "input_suf in",
            "output_suf ans",
            "n_subtasks 1",
            f"subtask_end_1 {test_count}",
            "subtask_score_1 100",
            f"time_limit {_time_limit_seconds(problem_config['time_limit_ms'])}",
            f"memory_limit {problem_config['memory_limit_mb']}",
            "use_pdf_statement on",
        )
    )
    if problem_config["mode"] == "interactive":
        lines.append("interaction_mode on")
    if problem_config["pass_limit"] == 2:
        lines.append("polygon_runtwice on")
        if problem_config["mode"] == "interactive":
            lines.extend(
                (
                    "polygon_runtwice_interactive on",
                    "interactor_run_type default",
                )
            )
    return "\n".join(lines) + "\n"


class QOJPackageAdapter(PackageAdapterSupport):
    """Write a source package consumed by QOJ Sync Test Data."""

    format: PackageFormat = "qoj"
    display_name = "QOJ"

    def plan(self, reader: NativePackageReader) -> PackageAdapterPlan:
        """Validate QOJ support and report advisory source omissions."""

        self._problem_config(reader)
        build_config = self.native_build_config(
            reader.root,
            problem_mode=reader.manifest["mode"],
        )
        accepted = self.configured_source(
            reader.root,
            build_config.get("accepted_solution_source"),
        )
        validator = self.configured_source(
            reader.root,
            build_config.get("validator_source"),
        )
        warning = ""
        if accepted is None or validator is None:
            warning = "QOJ Hack must be disabled until std and val are available."
        return PackageAdapterPlan(self.format, (), warning)

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        """Build one QOJ Sync Test Data source archive tree."""

        del canonical_problem_slug
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        problem_config = self._problem_config(reader)
        build_config = self.native_build_config(
            reader.root,
            problem_mode=reader.manifest["mode"],
        )
        self.prepare_target(target)

        builtin_checker = self._copy_programs(
            reader,
            target,
            build_config=build_config,
        )
        self._copy_tests(reader, target)
        self._write_statement_pdf(reader, target / "statement.pdf")
        self._copy_download_attachments(reader.root, target / "download")

        tests = reader.manifest["tests"]
        sample_count = sum(1 for test in tests if test["sample"])
        (target / "problem.conf").write_text(
            render_qoj_problem_conf(
                problem_config=problem_config,
                builtin_checker=builtin_checker,
                test_count=len(tests),
                sample_count=sample_count,
            ),
            encoding="utf-8",
            newline="\n",
        )
        return adapter_plan.warning

    def _problem_config(self, reader: NativePackageReader) -> ProblemConfig:
        pass_limit = reader.manifest["pass_limit"]
        if pass_limit > 2:
            raise ValueError("QOJ package supports at most two passes")
        config = self.native_problem_config(reader.root)
        if config["memory_limit_mb"] > QOJ_MEMORY_LIMIT_MB:
            raise ValueError(
                f"QOJ package memory limit exceeds {QOJ_MEMORY_LIMIT_MB} MiB"
            )
        return config

    def _copy_programs(
        self,
        reader: NativePackageReader,
        target: Path,
        *,
        build_config: BuildConfig,
    ) -> str | None:
        snapshot = reader.root
        accepted = self.configured_source(
            snapshot,
            build_config.get("accepted_solution_source"),
        )
        if accepted is not None:
            suffix = _QOJ_SOLUTION_SUFFIXES.get(accepted.suffix.lower())
            if suffix is None:
                raise ValueError(
                    "QOJ accepted solution language is unsupported: "
                    f"{accepted.suffix or accepted.name}"
                )
            shutil.copy2(accepted, target / f"std{suffix}")
        validator = self.configured_source(
            snapshot,
            build_config.get("validator_source"),
        )
        if validator is not None:
            self._copy_cpp_source(validator, target / "val.cpp", role="validator")

        if reader.manifest["mode"] == "interactive":
            interactor = cast(
                Path,
                self.configured_source(
                    snapshot,
                    build_config.get("interactor_source"),
                ),
            )
            self._copy_cpp_source(
                interactor,
                target / "interactor.cpp",
                role="interactor",
            )
            return "irscmp"

        checker = self.configured_source(
            snapshot,
            build_config.get("checker_source"),
        )
        if checker is None:
            (target / "chk.cpp").write_text(
                _EXACT_OUTPUT_CHECKER,
                encoding="utf-8",
                newline="\n",
            )
            return None
        standard_name = detect_standard_checker(checker)
        builtin = _QOJ_BUILTIN_CHECKERS.get(standard_name or "")
        if builtin is not None:
            return builtin
        self._copy_cpp_source(checker, target / "chk.cpp", role="checker")
        return None

    @staticmethod
    def _copy_cpp_source(
        source: Path,
        destination: Path,
        *,
        role: str,
    ) -> None:
        if source.suffix.lower() not in _CPP_SOURCE_SUFFIXES:
            raise ValueError(f"QOJ {role} must be C++ source: {source.name}")
        shutil.copy2(source, destination)

    @staticmethod
    def _copy_tests(reader: NativePackageReader, target: Path) -> None:
        samples: list[NativePackageTestEntry] = []
        for number, test in enumerate(reader.manifest["tests"], start=1):
            QOJPackageAdapter._copy_test_pair(
                reader,
                test,
                input_target=target / f"{number}.in",
                answer_target=target / f"{number}.ans",
                use_sample_payload=False,
            )
            if test["sample"]:
                samples.append(test)
        for number, test in enumerate(samples, start=1):
            QOJPackageAdapter._copy_test_pair(
                reader,
                test,
                input_target=target / f"ex_{number}.in",
                answer_target=target / f"ex_{number}.ans",
                use_sample_payload=True,
            )

    @staticmethod
    def _copy_test_pair(
        reader: NativePackageReader,
        test: NativePackageTestEntry,
        *,
        input_target: Path,
        answer_target: Path,
        use_sample_payload: bool,
    ) -> None:
        input_source = None
        if use_sample_payload:
            input_source = reader.payload(test, "sample_input")
        if input_source is None:
            input_source = cast(Path, reader.payload(test, "input"))
        shutil.copy2(input_source, input_target)
        answer_source = None
        if use_sample_payload:
            answer_source = reader.payload(test, "sample_output")
        if answer_source is None:
            answer_source = reader.payload(test, "answer")
        if answer_source is None:
            answer_target.write_bytes(b"")
            return
        shutil.copy2(answer_source, answer_target)

    @staticmethod
    def _copy_download_attachments(snapshot: Path, destination: Path) -> None:
        source_root = snapshot / "attachments"
        if not source_root.is_dir():
            return
        shutil.copytree(source_root, destination)

    def _write_statement_pdf(
        self,
        reader: NativePackageReader,
        destination: Path,
    ) -> None:
        languages = statement_languages(reader.root)
        language = "english" if "english" in languages else languages[0]
        entrypoint = (
            reader.root / STATEMENT_BUILD_DIR / language / "statements.tex"
        )
        result = self._tex_compile_service.compile_pdf(entrypoint)
        if result.proc.returncode != 0:
            error = self.statement_compile_error(result)
            raise ValueError(f"failed to compile {language} statement: {error}")
        pdf_path = result.pdf_path
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError(
                f"failed to compile {language} statement: PDF was not produced"
            )
        shutil.copy2(pdf_path, destination)
