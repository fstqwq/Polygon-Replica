"""Polygon full Linux package adapter."""

import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

from app.config import ConfigValues
from app.service.export.adapters.shared import (
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem.build_config import BuildConfig, parse_build_config
from app.service.problem.runtime_config import (
    ProblemConfig,
    parse_problem_config,
    problem_config_limits,
)
from app.service.problem.standard_checker import detect_standard_checker
from app.service.problem_package.manifest import (
    NativePackageSolutionEntry,
    NativePackageTestEntry,
)
from app.service.problem_package.service import NativePackageReader
from app.service.statement.context import (
    normalize_statement_language,
    statement_languages,
)
from app.service.statement.render import statement_title_from_snapshot
from app.service.statement.tex_compile import TexCompileService


_CPP_TYPE = "cpp.g++17"
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

_SOLUTION_TAGS = {
    "accepted": "accepted",
    "wrong_answer": "wrong-answer",
    "tle_or_correct": "time-limit-exceeded-or-accepted",
    "tle_or_re": "time-limit-exceeded-or-memory-limit-exceeded",
    "time_limit_exceeded": "time-limit-exceeded",
    "run_time_error": "memory-limit-exceeded",
    "compile_error": "rejected",
    "rejected": "rejected",
}


def _source_type(source: Path) -> str:
    suffix = source.suffix.lower()
    if suffix in {".c++", ".cc", ".cpp", ".cxx"}:
        return _CPP_TYPE
    if suffix == ".py":
        return "python.3"
    if suffix == ".java":
        return "java.17"
    raise ValueError(
        f"unsupported Polygon program language: {source.suffix or source.name}"
    )


def _safe_short_name(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return token or "problem"


def _section_text(snapshot: Path, language: str, filename: str) -> str:
    path = snapshot / "statement-sections" / language / filename
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _sample_text(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_bytes().decode("utf-8", errors="replace")


class PolygonLinuxPackageAdapter(PackageAdapterSupport):
    """Write a root-level Polygon full package for Linux consumers."""

    format: PackageFormat = "polygon-linux"
    display_name = "Polygon full package (Linux)"

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        super().__init__(config_values, tex_compile_service)

    def plan(self, reader: NativePackageReader) -> PackageAdapterPlan:
        """Validate Polygon metadata without narrowing its positive run count."""

        self._problem_config(reader)
        languages = self._language_folders(reader.root)
        warnings: list[str] = []
        pass_limit = reader.manifest["pass_limit"]
        if pass_limit > 1:
            warnings.append(
                "Some downstream converters do not preserve Polygon run-count."
            )
        if pass_limit > 2:
            warnings.append(
                f"Polygon run-count={pass_limit} is preserved; "
                "Polygon2DOMjudge currently converts only two-pass packages."
            )
        if pass_limit > 1 and reader.manifest["mode"] != "interactive":
            warnings.append(
                "Polygon2DOMjudge currently requires an interactor when "
                "converting a multi-pass package."
            )
        if len(languages) > 10:
            warnings.append(
                "Some downstream converters import at most ten statement languages."
            )
        if any(
            solution["expected_behavior"] == "unknown"
            for solution in reader.manifest["solutions"]
        ):
            warnings.append(
                "Solutions without an expected behavior are omitted because "
                "some downstream converters treat unknown Polygon tags as accepted."
            )
        return PackageAdapterPlan(
            self.format,
            tuple(reader.manifest["solutions"]),
            " ".join(warnings),
        )

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        self.prepare_target(target)

        snapshot = reader.root
        config = self._problem_config(reader)
        build_config = parse_build_config(
            (snapshot / "config" / "build.json").read_text(encoding="utf-8"),
            problem_mode=reader.manifest["mode"],
        )
        checker, interactor, validator = self._programs(
            snapshot,
            build_config,
            mode=reader.manifest["mode"],
        )
        language_folders = self._language_folders(snapshot)
        titles = self._write_statements(
            reader,
            target,
            language_folders=language_folders,
            fallback_title=self.problem_slug_leaf(canonical_problem_slug),
            config=config,
        )
        self._write_tests(reader, target)
        checker_name = self._write_programs(
            snapshot,
            target,
            checker=checker,
            interactor=interactor,
            validator=validator,
        )
        solution_rows = self._write_solutions(
            snapshot,
            target,
            solutions=adapter_plan.solutions,
            accepted_source=build_config.get("accepted_solution_source"),
        )
        attachments = self._write_attachments(snapshot, target)
        self._write_problem_xml(
            target,
            short_name=_safe_short_name(
                self.problem_slug_leaf(canonical_problem_slug)
            ),
            revision_number=reader.manifest["revision_number"],
            names=titles,
            language_folders=language_folders,
            config=config,
            tests=reader.manifest["tests"],
            checker_name=checker_name,
            has_checker=checker is not None or interactor is None,
            has_interactor=interactor is not None,
            has_validator=validator is not None,
            solutions=solution_rows,
            attachments=attachments,
        )
        return adapter_plan.warning

    def _problem_config(self, reader: NativePackageReader) -> ProblemConfig:
        return parse_problem_config(
            (reader.root / "config" / "problem.json").read_text(
                encoding="utf-8"
            ),
            limits=problem_config_limits(self._config_values),
        )

    @staticmethod
    def _programs(
        snapshot: Path,
        build_config: BuildConfig,
        *,
        mode: str,
    ) -> tuple[Path | None, Path | None, Path | None]:
        checker = (
            snapshot / build_config["checker_source"]
            if mode != "interactive" and "checker_source" in build_config
            else None
        )
        interactor = (
            snapshot / build_config["interactor_source"]
            if mode == "interactive" and "interactor_source" in build_config
            else None
        )
        validator = (
            snapshot / build_config["validator_source"]
            if "validator_source" in build_config
            else None
        )
        return checker, interactor, validator

    @staticmethod
    def _language_folders(snapshot: Path) -> tuple[tuple[str, str], ...]:
        rows: list[tuple[str, str]] = []
        observed: set[str] = set()
        for source_language in statement_languages(snapshot):
            folder = normalize_statement_language(source_language)
            if not folder:
                raise ValueError(
                    f"unsupported Polygon statement language: {source_language}"
                )
            if folder in observed:
                raise ValueError(
                    f"duplicate Polygon statement language folder: {folder}"
                )
            observed.add(folder)
            rows.append((source_language, folder))
        return tuple(rows)

    def _write_statements(
        self,
        reader: NativePackageReader,
        target: Path,
        *,
        language_folders: tuple[tuple[str, str], ...],
        fallback_title: str,
        config: ProblemConfig,
    ) -> dict[str, str]:
        snapshot = reader.root
        self._copy_statement_resources(snapshot, target)
        names: dict[str, str] = {}
        samples = self._sample_rows(reader)
        for source_language, folder in language_folders:
            build_root = snapshot / "statement-build" / source_language
            entrypoint = build_root / "statements.tex"
            statement_root = target / "statements" / folder
            shutil.copytree(build_root, statement_root)
            title = statement_title_from_snapshot(
                snapshot,
                fallback_title=fallback_title,
                language=source_language,
            )
            names[folder] = title
            properties = {
                "name": title,
                "language": folder,
                "legend": _section_text(snapshot, source_language, "legend.tex"),
                "input": _section_text(snapshot, source_language, "input.tex"),
                "output": _section_text(snapshot, source_language, "output.tex"),
                "interaction": _section_text(
                    snapshot,
                    source_language,
                    "interaction.tex",
                ),
                "notes": _section_text(snapshot, source_language, "notes.tex"),
                "sampleTests": [
                    {"input": sample_input, "output": sample_output}
                    for _test_index, sample_input, sample_output in samples
                ],
                "timeLimit": config["time_limit_ms"],
                "memoryLimit": config["memory_limit_mb"] * 1024 * 1024,
                "authorName": "",
                "authorLogin": "",
            }
            (statement_root / "problem-properties.json").write_text(
                json.dumps(properties, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            section_source = snapshot / "statement-sections" / source_language
            section_target = target / "statement-sections" / folder
            shutil.copytree(section_source, section_target)
            for test_index, sample_input, sample_output in samples:
                base = f"example.{test_index:02d}"
                for root in (statement_root, section_target):
                    (root / base).write_text(
                        sample_input,
                        encoding="utf-8",
                        newline="",
                    )
                    (root / f"{base}.a").write_text(
                        sample_output,
                        encoding="utf-8",
                        newline="",
                    )
            result = self._tex_compile_service.compile_pdf(entrypoint)
            if result.proc.returncode != 0:
                raise ValueError(
                    f"failed to compile {source_language} statement: "
                    f"{self.statement_compile_error(result)}"
                )
            pdf_target = target / "statements" / ".pdf" / folder / "problem.pdf"
            pdf_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(result.pdf_path, pdf_target)
        return names

    @staticmethod
    def _copy_statement_resources(snapshot: Path, target: Path) -> None:
        resources = {
            "statement/statements.ftl": "statements.ftl",
            "statement/problem.tex": "problem.tex",
            "statement/examples.tex": "examples.tex",
            "statement/olymp.sty": "olymp.sty",
        }
        files_root = target / "files"
        files_root.mkdir(parents=True)
        for relative, filename in resources.items():
            source = snapshot / relative
            if filename == "examples.tex" and not source.is_file():
                continue
            shutil.copy2(source, files_root / filename)

    @staticmethod
    def _sample_rows(reader: NativePackageReader) -> list[tuple[int, str, str]]:
        rows: list[tuple[int, str, str]] = []
        for index, test in enumerate(reader.manifest["tests"], start=1):
            if not test["sample"]:
                continue
            input_path = reader.payload(test, "sample_input") or reader.payload(
                test,
                "input",
            )
            output_path = reader.payload(test, "sample_output") or reader.payload(
                test,
                "answer",
            )
            rows.append(
                (index, _sample_text(input_path), _sample_text(output_path))
            )
        return rows

    @staticmethod
    def _write_tests(reader: NativePackageReader, target: Path) -> None:
        tests_root = target / "tests"
        tests_root.mkdir()
        for index, test in enumerate(reader.manifest["tests"], start=1):
            input_path = cast(Path, reader.payload(test, "input"))
            shutil.copy2(input_path, tests_root / f"{index:02d}")
            answer_path = reader.payload(test, "answer")
            answer_target = tests_root / f"{index:02d}.a"
            if answer_path is None:
                answer_target.write_bytes(b"")
            else:
                shutil.copy2(answer_path, answer_target)

    @staticmethod
    def _copy_testlib(snapshot: Path, target: Path) -> None:
        source = snapshot / "third_party" / "testlib" / "testlib.h"
        shutil.copy2(source, target / "files" / "testlib.h")

    def _write_programs(
        self,
        snapshot: Path,
        target: Path,
        *,
        checker: Path | None,
        interactor: Path | None,
        validator: Path | None,
    ) -> str:
        self._copy_testlib(snapshot, target)
        checker_name = ""
        if interactor is not None:
            shutil.copy2(interactor, target / "files" / "interactor.cpp")
        else:
            checker_target = target / "files" / "check.cpp"
            if checker is None:
                checker_target.write_text(
                    _EXACT_OUTPUT_CHECKER,
                    encoding="utf-8",
                    newline="\n",
                )
                checker_name = "polygon-replica-exact"
            else:
                shutil.copy2(checker, checker_target)
                standard_name = detect_standard_checker(checker)
                checker_name = (
                    f"std::{standard_name}"
                    if standard_name is not None
                    else "polygon-replica-custom"
                )
            shutil.copy2(checker_target, target / "check.cpp")
        if validator is not None:
            shutil.copy2(validator, target / "files" / "validator.cpp")
        return checker_name

    @staticmethod
    def _write_solutions(
        snapshot: Path,
        target: Path,
        *,
        solutions: tuple[NativePackageSolutionEntry, ...],
        accepted_source: str | None,
    ) -> tuple[tuple[str, str, str], ...]:
        rows: list[tuple[str, str, str]] = []
        observed: set[str] = set()
        solutions_root = target / "solutions"
        solutions_root.mkdir()
        for solution in solutions:
            if solution["expected_behavior"] == "unknown":
                continue
            source = snapshot / solution["source_path"]
            filename = source.name
            if filename in observed:
                filename = PackageAdapterSupport.unique_path(
                    solutions_root,
                    filename,
                ).name
            observed.add(filename)
            target_path = solutions_root / filename
            shutil.copy2(source, target_path)
            tag = _SOLUTION_TAGS[solution["expected_behavior"]]
            if solution["source_path"] == accepted_source:
                tag = "main"
            rows.append((f"solutions/{filename}", tag, _source_type(source)))
        return tuple(rows)

    @staticmethod
    def _write_attachments(snapshot: Path, target: Path) -> tuple[str, ...]:
        source_root = snapshot / "attachments"
        if not source_root.is_dir():
            return ()
        target_root = target / "attachments"
        shutil.copytree(source_root, target_root)
        return tuple(
            f"attachments/{path.relative_to(target_root).as_posix()}"
            for path in sorted(target_root.rglob("*"))
            if path.is_file()
        )

    @staticmethod
    def _write_problem_xml(
        target: Path,
        *,
        short_name: str,
        revision_number: int,
        names: dict[str, str],
        language_folders: tuple[tuple[str, str], ...],
        config: ProblemConfig,
        tests: list[NativePackageTestEntry],
        checker_name: str,
        has_checker: bool,
        has_interactor: bool,
        has_validator: bool,
        solutions: tuple[tuple[str, str, str], ...],
        attachments: tuple[str, ...],
    ) -> None:
        root = ET.Element(
            "problem",
            {"revision": str(revision_number), "short-name": short_name},
        )
        names_node = ET.SubElement(root, "names")
        for _source_language, folder in language_folders:
            ET.SubElement(
                names_node,
                "name",
                {"language": folder, "value": names[folder]},
            )
        statements_node = ET.SubElement(root, "statements")
        for _source_language, folder in language_folders:
            ET.SubElement(
                statements_node,
                "statement",
                {
                    "charset": "UTF-8",
                    "language": folder,
                    "path": f"statements/{folder}/problem.tex",
                    "type": "application/x-tex",
                },
            )
            ET.SubElement(
                statements_node,
                "statement",
                {
                    "language": folder,
                    "path": f"statements/.pdf/{folder}/problem.pdf",
                    "type": "application/pdf",
                },
            )
        judging = ET.SubElement(
            root,
            "judging",
            {
                "input-file": "",
                "output-file": "",
                "run-count": str(config["pass_limit"]),
            },
        )
        testset = ET.SubElement(judging, "testset", {"name": "tests"})
        ET.SubElement(testset, "time-limit").text = str(config["time_limit_ms"])
        ET.SubElement(testset, "memory-limit").text = str(
            config["memory_limit_mb"] * 1024 * 1024
        )
        ET.SubElement(testset, "test-count").text = str(len(tests))
        ET.SubElement(testset, "input-path-pattern").text = "tests/%02d"
        ET.SubElement(testset, "answer-path-pattern").text = "tests/%02d.a"
        tests_node = ET.SubElement(testset, "tests")
        for index, test in enumerate(tests, start=1):
            attributes = {
                "description": f'File "{index:02d}"',
                "method": "manual",
            }
            if test["sample"]:
                attributes["sample"] = "true"
            ET.SubElement(tests_node, "test", attributes)

        files_node = ET.SubElement(root, "files")
        resources = ET.SubElement(files_node, "resources")
        for filename in (
            "olymp.sty",
            "problem.tex",
            "statements.ftl",
            "testlib.h",
        ):
            ET.SubElement(resources, "file", {"path": f"files/{filename}"})
        if (target / "files" / "examples.tex").is_file():
            ET.SubElement(resources, "file", {"path": "files/examples.tex"})
        executables = ET.SubElement(files_node, "executables")
        executable_paths: list[str] = []
        if has_checker:
            executable_paths.append("files/check.cpp")
        if has_interactor:
            executable_paths.append("files/interactor.cpp")
        if has_validator:
            executable_paths.append("files/validator.cpp")
        for path in executable_paths:
            executable = ET.SubElement(executables, "executable")
            ET.SubElement(executable, "source", {"path": path, "type": _CPP_TYPE})
        if attachments:
            attachment_node = ET.SubElement(files_node, "attachments")
            for path in attachments:
                ET.SubElement(attachment_node, "file", {"path": path})

        assets = ET.SubElement(root, "assets")
        if has_checker:
            checker_node = ET.SubElement(
                assets,
                "checker",
                {"name": checker_name, "type": "testlib"},
            )
            ET.SubElement(
                checker_node,
                "source",
                {"path": "files/check.cpp", "type": _CPP_TYPE},
            )
            ET.SubElement(checker_node, "copy", {"path": "check.cpp"})
        if has_validator:
            validators = ET.SubElement(assets, "validators")
            validator_node = ET.SubElement(validators, "validator")
            ET.SubElement(
                validator_node,
                "source",
                {"path": "files/validator.cpp", "type": _CPP_TYPE},
            )
        if has_interactor:
            interactor = ET.SubElement(assets, "interactor")
            ET.SubElement(
                interactor,
                "source",
                {"path": "files/interactor.cpp", "type": _CPP_TYPE},
            )
            ET.SubElement(interactor, "copy", {"path": "interactor.cpp"})
            if config["pass_limit"] > 1:
                runs = ET.SubElement(interactor, "runs")
                for _index in range(config["pass_limit"]):
                    ET.SubElement(runs, "run")
        solutions_node = ET.SubElement(assets, "solutions")
        for path, tag, source_type in solutions:
            solution = ET.SubElement(solutions_node, "solution", {"tag": tag})
            ET.SubElement(
                solution,
                "source",
                {"path": path, "type": source_type},
            )
        properties = ET.SubElement(root, "properties")
        ET.SubElement(
            properties,
            "property",
            {"name": "tests-wellformed", "value": "true"},
        )
        if config["pass_limit"] > 1:
            ET.SubElement(
                properties,
                "property",
                {"name": "multipass", "value": "true"},
            )
        ET.indent(root, space="    ")
        ET.ElementTree(root).write(
            target / "problem.xml",
            encoding="utf-8",
            xml_declaration=True,
        )
