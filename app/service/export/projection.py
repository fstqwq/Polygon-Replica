"""Pure package projections from one validated problem revision.

The projector reads only the extracted, integrity-checked revision supplied by
its caller and writes one package tree below a caller-owned staging directory.
It does not discover source revisions, run verification, or persist jobs and
artifacts.
"""

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import ConfigValues
from app.service.export.icpc_package import (
    SUBMISSION_RULES,
    annotated_submission,
    render_domjudge_problem_yaml,
    render_problem_yaml,
    render_submissions_yaml,
    source_language,
    statement_language_code,
    write_input_validator,
    write_output_validator,
)
from app.service.problem.build_config import BuildConfig, load_build_config
from app.service.problem.runtime_config import load_problem_config, problem_config_limits
from app.service.problem.source_file import resolve_source
from app.service.problem_package.manifest import VerifiedSolutionEntry
from app.service.problem_package.service import VerifiedRevisionReader
from app.service.problem_package.statement_samples import hydrate_verified_statement_samples
from app.service.statement.context import statement_languages
from app.service.statement.render import render_statement_main
from app.service.statement.tex_compile import TexCompileService
from app.service.statement.title import statement_title_from_snapshot


PackageFormat = Literal["domjudge", "icpc-2025-09"]


@dataclass(frozen=True)
class ProjectionPlan:
    package_format: PackageFormat
    solutions: tuple[VerifiedSolutionEntry, ...]
    warning: str


class PackageProjectionService:
    """Project a verified revision into one externally defined package layout."""

    DOMJUDGE_COLOR_PALETTE = (
        "#e6194b",
        "#3cb44b",
        "#ffe119",
        "#4363d8",
        "#f58231",
        "#911eb4",
        "#46f0f0",
        "#f032e6",
        "#bcf60c",
        "#fabebe",
        "#008080",
        "#e6beff",
        "#9a6324",
        "#fffac8",
        "#800000",
        "#aaffc3",
        "#808000",
        "#ffd8b1",
    )

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        self._config_values = config_values
        self._tex_compile_service = tex_compile_service

    def project(
        self,
        reader: VerifiedRevisionReader,
        *,
        package_format: PackageFormat,
        target: Path,
        canonical_problem_slug: str,
        short_name: str | None = None,
        plan: ProjectionPlan | None = None,
    ) -> str:
        """Write exactly one requested package layout to an empty target directory."""

        if package_format not in {"domjudge", "icpc-2025-09"}:
            raise ValueError(f"unsupported package format: {package_format}")
        projection_plan = plan or self.plan(reader, package_format=package_format)
        if projection_plan.package_format != package_format:
            raise ValueError("package projection plan format does not match request")
        if package_format == "domjudge":
            resolved_short_name = self._domjudge_short_name(short_name)
            self.project_domjudge(
                reader,
                target=target,
                canonical_problem_slug=canonical_problem_slug,
                short_name=resolved_short_name,
                plan=projection_plan,
            )
            return projection_plan.warning
        if short_name is not None:
            raise ValueError("ICPC 2025-09 projection does not accept a DOMjudge short-name")
        self.project_icpc_2025(
            reader,
            target=target,
            canonical_problem_slug=canonical_problem_slug,
            plan=projection_plan,
        )
        return projection_plan.warning

    @staticmethod
    def plan(
        reader: VerifiedRevisionReader,
        *,
        package_format: PackageFormat,
    ) -> ProjectionPlan:
        solutions = tuple(reader.manifest["solutions"])
        if package_format == "domjudge":
            return ProjectionPlan(package_format, solutions, "")
        if package_format != "icpc-2025-09":
            raise ValueError(f"unsupported package format: {package_format}")
        omitted = tuple(
            solution["source_path"]
            for solution in solutions
            if "CE" in solution["verdicts"]
        )
        selected = tuple(
            solution
            for solution in solutions
            if solution["source_path"] not in omitted
        )
        warning = (
            "ICPC 2025-09 omitted submissions with compile-error results: "
            + ", ".join(omitted)
            if omitted
            else ""
        )
        return ProjectionPlan(package_format, selected, warning)

    def project_icpc_2025(
        self,
        reader: VerifiedRevisionReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: ProjectionPlan | None = None,
    ) -> str:
        """Write a strict ICPC Problem Package 2025-09 tree."""

        self._prepare_target(target)
        projection_plan = plan or self.plan(reader, package_format="icpc-2025-09")
        if projection_plan.package_format != "icpc-2025-09":
            raise ValueError("package projection plan format does not match request")
        self._build_icpc_2025(
            reader,
            target=target,
            canonical_problem_slug=canonical_problem_slug,
            plan=projection_plan,
        )
        return projection_plan.warning

    def project_domjudge(
        self,
        reader: VerifiedRevisionReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        short_name: str,
        plan: ProjectionPlan | None = None,
    ) -> str:
        """Write a DOMjudge-compatible problem package tree."""

        self._prepare_target(target)
        projection_plan = plan or self.plan(reader, package_format="domjudge")
        if projection_plan.package_format != "domjudge":
            raise ValueError("package projection plan format does not match request")
        self._build_domjudge(
            reader,
            target=target,
            canonical_problem_slug=canonical_problem_slug,
            short_name=self._domjudge_short_name(short_name),
            plan=projection_plan,
        )
        return projection_plan.warning

    @staticmethod
    def _prepare_target(target: Path) -> None:
        if target.is_symlink():
            raise ValueError("package projection target must not be a symbolic link")
        if target.exists():
            if not target.is_dir():
                raise ValueError("package projection target must be a directory")
            if any(target.iterdir()):
                raise ValueError("package projection target must be empty")
            return
        target.mkdir(parents=True)

    def _build_icpc_2025(
        self,
        reader: VerifiedRevisionReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: ProjectionPlan,
    ) -> None:
        snapshot = reader.root
        mode = reader.manifest["mode"]
        pass_limit = reader.manifest["pass_limit"]
        problem_name = statement_title_from_snapshot(
            snapshot,
            fallback_title=self._problem_slug_leaf(canonical_problem_slug),
        )
        build_config = load_build_config(snapshot)
        checker, interactor, validator = self._projection_programs(
            snapshot,
            build_config,
            mode=mode,
        )
        self._hydrate_statement_samples(reader)
        statement_names = self._write_statements(
            snapshot,
            target / "statement",
            problem_name=problem_name,
            include_sample_tests=not self._samples_are_secret(mode, pass_limit),
            keep_all_languages=True,
        )
        problem_config = load_problem_config(
            snapshot,
            limits=problem_config_limits(self._config_values),
        )
        (target / "problem.yaml").write_text(
            render_problem_yaml(
                problem_slug=canonical_problem_slug,
                source_commit=reader.verified_revision["source_commit"],
                names=statement_names,
                mode=mode,
                pass_limit=pass_limit,
                time_limit_ms=problem_config["time_limit_ms"],
                memory_limit_mb=problem_config["memory_limit_mb"],
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._copy_test_data(
            reader,
            target,
            samples_as_secret=self._samples_are_secret(mode, pass_limit),
        )
        self._write_validators(
            snapshot,
            target,
            validator=validator,
            output_validator=interactor if mode == "interactive" else checker,
        )
        self._copy_submissions(
            snapshot,
            target,
            solutions=plan.solutions,
            include_submissions_yaml=True,
            annotate_mixed=False,
        )
        self._copy_attachments(snapshot, target)

    def _build_domjudge(
        self,
        reader: VerifiedRevisionReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        short_name: str,
        plan: ProjectionPlan,
    ) -> None:
        snapshot = reader.root
        mode = reader.manifest["mode"]
        pass_limit = reader.manifest["pass_limit"]
        problem_name = statement_title_from_snapshot(
            snapshot,
            fallback_title=self._problem_slug_leaf(canonical_problem_slug),
        )
        build_config = load_build_config(snapshot)
        checker, interactor, validator = self._projection_programs(
            snapshot,
            build_config,
            mode=mode,
        )
        self._hydrate_statement_samples(reader)
        statement_names = self._write_statements(
            snapshot,
            target / "problem_statement",
            problem_name=problem_name,
            include_sample_tests=not self._samples_are_secret(mode, pass_limit),
            keep_all_languages=False,
        )
        problem_config = load_problem_config(
            snapshot,
            limits=problem_config_limits(self._config_values),
        )
        (target / "problem.yaml").write_text(
            render_domjudge_problem_yaml(
                names=statement_names,
                mode=mode,
                pass_limit=pass_limit,
                time_limit_ms=problem_config["time_limit_ms"],
                memory_limit_mb=problem_config["memory_limit_mb"],
            ),
            encoding="utf-8",
            newline="\n",
        )
        (target / "domjudge-problem.ini").write_text(
            self._domjudge_problem_ini(
                problem_name=problem_name,
                external_id=self._domjudge_external_id(canonical_problem_slug),
                short_name=short_name,
                time_limit_ms=problem_config["time_limit_ms"],
            ),
            encoding="utf-8",
            newline="\n",
        )
        self._copy_test_data(
            reader,
            target,
            samples_as_secret=self._samples_are_secret(mode, pass_limit),
        )
        self._write_validators(
            snapshot,
            target,
            validator=validator,
            output_validator=interactor if mode == "interactive" else checker,
        )
        self._copy_submissions(
            snapshot,
            target,
            solutions=plan.solutions,
            include_submissions_yaml=False,
            annotate_mixed=True,
        )
        self._copy_attachments(snapshot, target)

    def _hydrate_statement_samples(self, reader: VerifiedRevisionReader) -> None:
        limits = self._config_values.snapshot()
        hydrate_verified_statement_samples(
            reader,
            tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
            statement_sample_max_bytes=int(limits["STATEMENT_SAMPLE_MAX_BYTES"]),
        )

    def _write_statements(
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
            raise ValueError("package projection requires at least one problem statement")
        limits = self._config_values.snapshot()
        compiled: dict[str, bytes] = {}
        names: dict[str, str] = {}
        for language in languages:
            language_code = statement_language_code(language)
            if language_code in compiled:
                raise ValueError(f"duplicate statement language code: {language_code}")
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
                raise ValueError(f"failed to render {language} statement: {exc}") from exc
            compile_result = self._tex_compile_service.compile_pdf(rendered)
            if compile_result.proc.returncode != 0:
                error = str(
                    compile_result.proc.stderr
                    or compile_result.proc.stdout
                    or "statement compiler failed"
                ).strip()
                raise ValueError(f"failed to compile {language} statement: {error}")
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
                (destination / f"problem.{language_code}.pdf").write_bytes(payload)
        else:
            preferred = "en" if "en" in compiled else next(iter(compiled))
            (destination / "problem.pdf").write_bytes(compiled[preferred])
        return names

    @staticmethod
    def _samples_are_secret(mode: str, pass_limit: int) -> bool:
        return mode == "interactive" or pass_limit > 1

    @staticmethod
    def _copy_test_data(
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
            destination = secret_dir if samples_as_secret or not row["sample"] else sample_dir
            input_source = reader.payload(row, "input")
            if destination == sample_dir:
                input_source = reader.payload(row, "sample_input") or input_source
            if input_source is None:
                raise ValueError(f"verified test input is missing: {test_id}")
            shutil.copy2(input_source, destination / f"{test_id}.in")
            answer_source = reader.payload(row, "answer")
            if destination == sample_dir:
                answer_source = reader.payload(row, "sample_output") or answer_source
            answer_target = destination / f"{test_id}.ans"
            if answer_source is None:
                if reader.manifest["mode"] != "interactive":
                    raise ValueError(f"verified test answer is missing: {test_id}")
                answer_target.write_bytes(b"")
            else:
                shutil.copy2(answer_source, answer_target)

    def _projection_programs(
        self,
        snapshot: Path,
        build_config: BuildConfig,
        *,
        mode: str,
    ) -> tuple[Path | None, Path | None, Path | None]:
        checker = None
        interactor = None
        if mode == "interactive":
            interactor = self._configured_source(
                snapshot,
                build_config.get("interactor_source"),
            )
            if interactor is None:
                raise ValueError("interactive package projection requires an interactor")
        else:
            checker = self._configured_source(
                snapshot,
                build_config.get("checker_source"),
            )
        validator = self._configured_source(
            snapshot,
            build_config.get("validator_source"),
        )
        return checker, interactor, validator

    @staticmethod
    def _configured_source(snapshot: Path, rel_path: str | None) -> Path | None:
        if rel_path is None:
            return None
        try:
            return resolve_source(snapshot, rel_path)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc

    @staticmethod
    def _write_validators(
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

    def _copy_submissions(
        self,
        snapshot: Path,
        package_root: Path,
        *,
        solutions: tuple[VerifiedSolutionEntry, ...],
        include_submissions_yaml: bool,
        annotate_mixed: bool,
    ) -> None:
        submissions = package_root / "submissions"
        submissions.mkdir(parents=True)
        metadata: dict[str, dict[str, object]] = {}
        accepted_count = 0
        for solution in solutions:
            source_rel = solution["source_path"]
            source_file = resolve_source(snapshot, source_rel)
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
            target = self._unique_path(target_dir, source_file.name)
            if annotate_mixed and expected in {"tle_or_correct", "tle_or_re", "rejected"}:
                target.write_bytes(
                    annotated_submission(source_file, rule["domjudge_results"])
                )
            else:
                shutil.copy2(source_file, target)
            if include_submissions_yaml:
                rel = target.relative_to(submissions).as_posix()
                metadata[rel] = {
                    "language": source_language(source_file),
                    "permitted": list(rule["permitted"]),
                    "required": list(rule["required"]),
                }
            if expected == "accepted":
                accepted_count += 1
        if accepted_count == 0:
            raise ValueError("package projection requires at least one accepted submission")
        if include_submissions_yaml:
            (submissions / "submissions.yaml").write_text(
                render_submissions_yaml(metadata),
                encoding="utf-8",
                newline="\n",
            )

    @staticmethod
    def _unique_path(parent: Path, filename: str) -> Path:
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
    def _copy_attachments(snapshot: Path, package_root: Path) -> None:
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
                name for name in directories if not (current / name).is_symlink()
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
    def _problem_slug_leaf(canonical_problem_slug: str) -> str:
        leaf = canonical_problem_slug.replace("\\", "/").strip("/").rsplit("/", 1)[-1]
        return leaf or "problem"

    @staticmethod
    def _domjudge_short_name(value: str | None) -> str:
        if value is None or not value:
            raise ValueError("DOMjudge short-name is required")
        if "\n" in value or "\r" in value:
            raise ValueError("DOMjudge short-name must be a single line")
        return value

    @classmethod
    def _domjudge_external_id(cls, canonical_problem_slug: str) -> str:
        token = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            cls._problem_slug_leaf(canonical_problem_slug),
        ).strip("-.")
        return token or "problem"

    @staticmethod
    def _ini_value(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9 .,_/-]+", value):
            return value
        escaped = value.replace("\\", "\\\\").replace('"', r'\"')
        return f'"{escaped}"'

    def _domjudge_problem_ini(
        self,
        *,
        problem_name: str,
        external_id: str,
        short_name: str,
        time_limit_ms: int,
    ) -> str:
        seconds = time_limit_ms / 1000.0
        digest = hashlib.sha256(external_id.encode("utf-8")).digest()
        color = self.DOMJUDGE_COLOR_PALETTE[
            digest[0] % len(self.DOMJUDGE_COLOR_PALETTE)
        ]
        return (
            f"name = {self._ini_value(problem_name)}\n"
            f"externalid = {self._ini_value(external_id)}\n"
            f"short-name = {self._ini_value(short_name)}\n"
            f"timelimit = {seconds:.3f}".rstrip("0").rstrip(".")
            + "\n"
            f"color = {color}\n"
        )
