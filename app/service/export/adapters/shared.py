"""Shared contract and mechanics for package adapters.

Format policy belongs in the individual adapter modules. This module only
contains operations that have identical meaning for every supported package:
reading verified test payloads, compiling statements, copying source files,
and preparing a safe caller-owned target directory.
"""

import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from app.config import ConfigValues
from app.service.problem.build_config import BuildConfig
from app.service.problem.runtime_config import problem_config_limits
from app.service.problem.source_file import resolve_source
from app.service.problem_package.manifest import VerifiedSolutionEntry
from app.service.problem_package.service import VerifiedRevisionReader
from app.service.problem_package.statement_samples import (
    hydrate_verified_statement_samples,
)
from app.service.statement.context import statement_languages
from app.service.statement.render import (
    render_statement_main,
    statement_title_from_snapshot,
)
from app.service.statement.tex_compile import TexCompileService


type PackageFormat = str


@dataclass(frozen=True)
class PackageAdapterPlan:
    package_format: PackageFormat
    solutions: tuple[VerifiedSolutionEntry, ...]
    warning: str


class PackageAdapter(Protocol):
    """One package format, including selection policy and filesystem layout."""

    format: PackageFormat
    display_name: str
    accepts_short_name: bool

    def plan(self, reader: VerifiedRevisionReader) -> PackageAdapterPlan: ...

    def build(
        self,
        reader: VerifiedRevisionReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        short_name: str | None = None,
        plan: PackageAdapterPlan | None = None,
    ) -> str: ...


_LANGUAGE_CODES = {
    "english": "en",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "portuguese": "pt",
}

_SOURCE_LANGUAGE = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".py": "python3",
    ".java": "java",
}


class SubmissionRule(TypedDict):
    ppf_directory: str
    domjudge_directory: str
    permitted: tuple[str, ...]
    required: tuple[str, ...]
    domjudge_results: tuple[str, ...]


SUBMISSION_RULES: dict[str, SubmissionRule] = {
    "accepted": {
        "ppf_directory": "accepted",
        "domjudge_directory": "accepted",
        "permitted": ("AC",),
        "required": ("AC",),
        "domjudge_results": ("CORRECT",),
    },
    "wrong_answer": {
        "ppf_directory": "wrong_answer",
        "domjudge_directory": "wrong_answer",
        "permitted": ("AC", "WA"),
        "required": ("WA",),
        "domjudge_results": ("CORRECT", "WRONG-ANSWER"),
    },
    "time_limit_exceeded": {
        "ppf_directory": "time_limit_exceeded",
        "domjudge_directory": "time_limit_exceeded",
        "permitted": ("AC", "TLE"),
        "required": ("TLE",),
        "domjudge_results": ("CORRECT", "TIMELIMIT"),
    },
    "run_time_error": {
        "ppf_directory": "run_time_error",
        "domjudge_directory": "run_time_error",
        "permitted": ("AC", "RTE"),
        "required": ("RTE",),
        "domjudge_results": ("CORRECT", "RUN-ERROR"),
    },
    "tle_or_correct": {
        "ppf_directory": "mixed_tle_or_correct",
        "domjudge_directory": "mixed",
        "permitted": ("AC", "TLE"),
        "required": ("AC", "TLE"),
        "domjudge_results": ("CORRECT", "TIMELIMIT"),
    },
    "tle_or_re": {
        "ppf_directory": "mixed_tle_or_re",
        "domjudge_directory": "mixed",
        "permitted": ("TLE", "RTE"),
        "required": ("TLE", "RTE"),
        "domjudge_results": ("TIMELIMIT", "RUN-ERROR"),
    },
    "rejected": {
        "ppf_directory": "mixed_rejected",
        "domjudge_directory": "mixed",
        "permitted": ("AC", "WA", "TLE", "RTE"),
        "required": ("WA", "TLE", "RTE"),
        "domjudge_results": (
            "WRONG-ANSWER",
            "TIMELIMIT",
            "RUN-ERROR",
            "COMPILER-ERROR",
        ),
    },
}


def statement_language_code(language: str) -> str:
    token = language.strip().replace("_", "-")
    mapped = _LANGUAGE_CODES.get(token.lower())
    if mapped is not None:
        return mapped
    parts = token.split("-")
    if len(parts[0]) not in {2, 3} or not parts[0].isalpha():
        raise ValueError(f"unsupported statement language: {language}")
    if len(parts) == 1:
        return parts[0].lower()
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        return f"{parts[0].lower()}-{parts[1].upper()}"
    raise ValueError(f"unsupported statement language: {language}")


def source_language(path: Path) -> str:
    language = _SOURCE_LANGUAGE.get(path.suffix.lower())
    if language is None:
        raise ValueError(
            f"unsupported ICPC program language: {path.suffix or path.name}"
        )
    return language


def annotated_submission(source: Path, results: tuple[str, ...]) -> bytes:
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise ValueError(
            f"submission source is not UTF-8: {source.name}"
        ) from exc
    if "@EXPECTED_RESULTS@:" in text.upper():
        raise ValueError(
            f"submission already contains @EXPECTED_RESULTS@: {source.name}"
        )
    comment = "#" if source.suffix.lower() == ".py" else "//"
    annotation = f"{comment} @EXPECTED_RESULTS@: {','.join(results)}\n"
    return (annotation + text).encode("utf-8")


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _copy_testlib(snapshot: Path, target_dir: Path) -> None:
    testlib = snapshot / "third_party" / "testlib" / "testlib.h"
    if testlib.is_symlink() or not testlib.is_file():
        raise ValueError(
            "export missing testlib header: third_party/testlib/testlib.h"
        )
    shutil.copy2(testlib, target_dir / "testlib.h")


def _program_command(
    source: Path,
    *,
    snapshot: Path,
    target_dir: Path,
) -> tuple[str, str]:
    suffix = source.suffix.lower()
    shutil.copy2(source, target_dir / source.name)
    quoted_name = shlex.quote(f"./{source.name}")
    if suffix in {".cc", ".cpp", ".cxx", ".c++"}:
        _copy_testlib(snapshot, target_dir)
        return (
            f"c++ -Wall -DDOMJUDGE -O2 -std=gnu++20 -o program.bin {quoted_name}\n",
            '"$program_dir/program.bin"',
        )
    if suffix == ".c":
        return (
            f"cc -Wall -DDOMJUDGE -O2 -o program.bin {quoted_name}\n",
            '"$program_dir/program.bin"',
        )
    if suffix == ".py":
        return ("", f'python3 "$program_dir"/{shlex.quote(source.name)}')
    raise ValueError(
        f"unsupported ICPC validator language: {suffix or source.name}"
    )


def write_input_validator(
    *,
    snapshot: Path,
    package_root: Path,
    source: Path | None,
) -> None:
    validator_name = "validator" if source is not None else "accept_all"
    target_dir = package_root / "input_validators" / validator_name
    target_dir.mkdir(parents=True, exist_ok=True)
    if source is None:
        _write_executable(
            target_dir / "run",
            "#!/bin/sh\ncat >/dev/null\nexit 42\n",
        )
        return
    build_command, command = _program_command(
        source,
        snapshot=snapshot,
        target_dir=target_dir,
    )
    if build_command:
        _write_executable(
            target_dir / "build",
            "#!/bin/sh\nset -eu\ncd -- \"$(dirname -- \"$0\")\"\n"
            + build_command,
        )
    _write_executable(
        target_dir / "run",
        "#!/bin/sh\n"
        "set +e\n"
        "program_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        f"{command} \"$@\"\n"
        "status=$?\n"
        "case \"$status\" in\n"
        "  0|42) exit 42 ;;\n"
        "  *) exit \"$status\" ;;\n"
        "esac\n",
    )


def write_output_validator(
    *,
    snapshot: Path,
    package_root: Path,
    source: Path | None,
) -> None:
    if source is None:
        return
    target_dir = package_root / "output_validator"
    target_dir.mkdir(parents=True, exist_ok=True)
    build_command, command = _program_command(
        source,
        snapshot=snapshot,
        target_dir=target_dir,
    )
    if build_command:
        _write_executable(
            target_dir / "build",
            "#!/bin/sh\nset -eu\ncd -- \"$(dirname -- \"$0\")\"\n"
            + build_command,
        )
    _write_executable(
        target_dir / "run",
        "#!/bin/sh\n"
        "set +e\n"
        "program_dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
        f"{command} \"$@\"\n"
        "status=$?\n"
        "case \"$status\" in\n"
        "  0|42) exit 42 ;;\n"
        "  1|2|43) exit 43 ;;\n"
        "  *) exit \"$status\" ;;\n"
        "esac\n",
    )


class PackageAdapterSupport:
    """Format-neutral filesystem operations used by concrete adapters."""

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        self._config_values = config_values
        self._tex_compile_service = tex_compile_service

    @staticmethod
    def prepare_target(target: Path) -> None:
        if target.is_symlink():
            raise ValueError("package adapter target must not be a symbolic link")
        if target.exists():
            if not target.is_dir():
                raise ValueError("package adapter target must be a directory")
            if any(target.iterdir()):
                raise ValueError("package adapter target must be empty")
            return
        target.mkdir(parents=True)

    def hydrate_statement_samples(self, reader: VerifiedRevisionReader) -> None:
        limits = self._config_values.snapshot()
        hydrate_verified_statement_samples(
            reader,
            tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(
                limits["STATEMENT_SAMPLE_MAX_BYTES"]
            ),
        )

    def write_statements(
        self,
        snapshot: Path,
        destination: Path,
        *,
        problem_name: str,
        include_sample_tests: bool,
        keep_all_languages: bool,
    ) -> dict[str, str]:
        languages = statement_languages(snapshot)
        if not languages:
            raise ValueError(
                "package adapter requires at least one problem statement"
            )
        limits = self._config_values.snapshot()
        compiled: dict[str, bytes] = {}
        names: dict[str, str] = {}
        for language in languages:
            language_code = statement_language_code(language)
            if language_code in compiled:
                raise ValueError(
                    f"duplicate statement language code: {language_code}"
                )
            try:
                rendered = render_statement_main(
                    snapshot / "statement",
                    problem_title=problem_name,
                    language=language,
                    include_sample_tests=include_sample_tests,
                    tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
                    statement_sample_max_bytes=int(
                        limits["STATEMENT_SAMPLE_MAX_BYTES"]
                    ),
                    problem_limits=problem_config_limits(self._config_values),
                )
            except Exception as exc:
                raise ValueError(
                    f"failed to render {language} statement: {exc}"
                ) from exc
            compile_result = self._tex_compile_service.compile_pdf(rendered)
            if compile_result.proc.returncode != 0:
                error = str(
                    compile_result.proc.stderr
                    or compile_result.proc.stdout
                    or "statement compiler failed"
                ).strip()
                raise ValueError(
                    f"failed to compile {language} statement: {error}"
                )
            pdf_path = compile_result.pdf_path
            if pdf_path.is_symlink() or not pdf_path.is_file():
                raise ValueError(
                    f"failed to compile {language} statement: PDF was not produced"
                )
            compiled[language_code] = pdf_path.read_bytes()
            names[language_code] = statement_title_from_snapshot(
                snapshot,
                fallback_title=problem_name,
                language=language,
            )
        destination.mkdir(parents=True)
        if keep_all_languages:
            for language_code, payload in compiled.items():
                (destination / f"problem.{language_code}.pdf").write_bytes(
                    payload
                )
        else:
            preferred = "en" if "en" in compiled else next(iter(compiled))
            (destination / "problem.pdf").write_bytes(compiled[preferred])
        return names

    @staticmethod
    def samples_are_secret(mode: str, pass_limit: int) -> bool:
        return mode == "interactive" or pass_limit > 1

    @staticmethod
    def copy_test_data(
        reader: VerifiedRevisionReader,
        package_root: Path,
        *,
        samples_as_secret: bool,
    ) -> None:
        secret_dir = package_root / "data" / "secret"
        sample_dir = package_root / "data" / "sample"
        secret_dir.mkdir(parents=True)
        sample_dir.mkdir(parents=True)
        for row in reader.manifest["tests"]:
            test_id = row["id"]
            destination = (
                secret_dir
                if samples_as_secret or not row["sample"]
                else sample_dir
            )
            input_source = reader.payload(row, "input")
            if destination == sample_dir:
                input_source = reader.payload(row, "sample_input") or input_source
            if input_source is None:
                raise ValueError(f"verified test input is missing: {test_id}")
            shutil.copy2(input_source, destination / f"{test_id}.in")
            answer_source = reader.payload(row, "answer")
            if destination == sample_dir:
                answer_source = (
                    reader.payload(row, "sample_output") or answer_source
                )
            answer_target = destination / f"{test_id}.ans"
            if answer_source is None:
                if reader.manifest["mode"] != "interactive":
                    raise ValueError(
                        f"verified test answer is missing: {test_id}"
                    )
                answer_target.write_bytes(b"")
            else:
                shutil.copy2(answer_source, answer_target)

    def package_programs(
        self,
        snapshot: Path,
        build_config: BuildConfig,
        *,
        mode: str,
    ) -> tuple[Path | None, Path | None, Path | None]:
        checker = None
        interactor = None
        if mode == "interactive":
            interactor = self.configured_source(
                snapshot,
                build_config.get("interactor_source"),
            )
            if interactor is None:
                raise ValueError(
                    "interactive package adapter requires an interactor"
                )
        else:
            checker = self.configured_source(
                snapshot,
                build_config.get("checker_source"),
            )
        validator = self.configured_source(
            snapshot,
            build_config.get("validator_source"),
        )
        return checker, interactor, validator

    @staticmethod
    def configured_source(snapshot: Path, rel_path: str | None) -> Path | None:
        if rel_path is None:
            return None
        try:
            return resolve_source(snapshot, rel_path)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def write_validators(
        snapshot: Path,
        package_root: Path,
        *,
        validator: Path | None,
        output_validator: Path | None,
    ) -> None:
        write_input_validator(
            snapshot=snapshot,
            package_root=package_root,
            source=validator,
        )
        write_output_validator(
            snapshot=snapshot,
            package_root=package_root,
            source=output_validator,
        )

    @staticmethod
    def copy_submissions(
        snapshot: Path,
        package_root: Path,
        *,
        solutions: tuple[VerifiedSolutionEntry, ...],
        collect_metadata: bool,
        annotate_mixed: bool,
    ) -> dict[str, dict[str, object]]:
        submissions = package_root / "submissions"
        submissions.mkdir(parents=True)
        metadata: dict[str, dict[str, object]] = {}
        accepted_count = 0
        for solution in solutions:
            source_file = resolve_source(snapshot, solution["source_path"])
            expected = solution["expected_behavior"]
            rule = SUBMISSION_RULES.get(expected)
            if rule is None:
                continue
            directory = (
                rule["domjudge_directory"]
                if annotate_mixed
                else rule["ppf_directory"]
            )
            target_dir = submissions / directory
            target_dir.mkdir(parents=True, exist_ok=True)
            target = PackageAdapterSupport.unique_path(
                target_dir,
                source_file.name,
            )
            if (
                annotate_mixed
                and expected in {"tle_or_correct", "tle_or_re", "rejected"}
            ):
                target.write_bytes(
                    annotated_submission(source_file, rule["domjudge_results"])
                )
            else:
                shutil.copy2(source_file, target)
            if collect_metadata:
                rel = target.relative_to(submissions).as_posix()
                metadata[rel] = {
                    "language": source_language(source_file),
                    "permitted": list(rule["permitted"]),
                    "required": list(rule["required"]),
                }
            if expected == "accepted":
                accepted_count += 1
        if accepted_count == 0:
            raise ValueError(
                "package adapter requires at least one accepted submission"
            )
        return metadata

    @staticmethod
    def unique_path(parent: Path, filename: str) -> Path:
        safe_name = Path(filename).name or "solution.cpp"
        target = parent / safe_name
        if not target.exists():
            return target
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        index = 2
        while (candidate := parent / f"{stem}-{index}{suffix}").exists():
            index += 1
        return candidate

    @staticmethod
    def copy_attachments(snapshot: Path, package_root: Path) -> None:
        source_root = snapshot / "attachments"
        if source_root.is_symlink() or not source_root.is_dir():
            return
        destination_root = package_root / "attachments"
        source_resolved = source_root.resolve()
        for directory, directories, filenames in os.walk(
            source_root,
            topdown=True,
            followlinks=False,
        ):
            current = Path(directory)
            directories[:] = sorted(
                name
                for name in directories
                if not (current / name).is_symlink()
            )
            for name in sorted(filenames):
                source = current / name
                if source.is_symlink() or not source.is_file():
                    continue
                resolved = source.resolve()
                if source_resolved not in resolved.parents:
                    continue
                target = destination_root / source.relative_to(source_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    @staticmethod
    def problem_slug_leaf(canonical_problem_slug: str) -> str:
        leaf = (
            canonical_problem_slug.replace("\\", "/")
            .strip("/")
            .rsplit("/", 1)[-1]
        )
        return leaf or "problem"
