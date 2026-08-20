"""ICPC Problem Package 2025-09 adapter."""

import uuid
from pathlib import Path

import yaml

from app.config import ConfigValues
from app.service.export.adapters.shared import (
    ContestPackagePlacement,
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem.build_config import load_build_config
from app.service.problem.runtime_config import (
    load_problem_config,
    problem_config_limits,
)
from app.service.problem_package.service import NativePackageReader
from app.service.statement.render import statement_title_from_snapshot
from app.service.statement.tex_compile import TexCompileService


def problem_uuid(problem_slug: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"polygon-replica/problem/{problem_slug}",
        )
    )


def problem_type(*, mode: str, pass_limit: int) -> str | list[str]:
    """Return the PPF 2025-09 type for the canonical execution mode."""

    parts = ["pass-fail"]
    if mode == "interactive":
        parts.append("interactive")
    if pass_limit > 1:
        parts.append("multi-pass")
    return parts[0] if len(parts) == 1 else parts


def render_problem_yaml(
    *,
    problem_slug: str,
    source_commit: str,
    names: dict[str, str],
    mode: str,
    pass_limit: int,
    time_limit_ms: int,
    memory_limit_mb: int | None,
) -> str:
    if not names:
        raise ValueError("ICPC export requires at least one problem statement")
    type_value = problem_type(mode=mode, pass_limit=pass_limit)
    name_value: str | dict[str, str]
    if list(names) == ["en"]:
        name_value = names["en"]
    else:
        name_value = names
    limits: dict[str, object] = {
        "time_limit": max(0.001, time_limit_ms / 1000.0),
    }
    if memory_limit_mb is not None:
        limits["memory"] = memory_limit_mb
    if pass_limit > 1:
        limits["validation_passes"] = pass_limit
    payload: dict[str, object] = {
        "problem_format_version": "2025-09",
        "type": type_value,
        "name": name_value,
        "uuid": problem_uuid(problem_slug),
        "version": source_commit,
        "limits": limits,
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


def render_submissions_yaml(entries: dict[str, dict[str, object]]) -> str:
    return yaml.safe_dump(
        entries,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


class ICPC2025PackageAdapter(PackageAdapterSupport):
    """Write one strict ICPC Problem Package 2025-09 tree."""

    format: PackageFormat = "icpc-2025-09"
    display_name = "ICPC 2025-09"

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        super().__init__(config_values, tex_compile_service)

    @staticmethod
    def plan(reader: NativePackageReader) -> PackageAdapterPlan:
        solutions = tuple(reader.manifest["solutions"])
        omitted = tuple(
            solution["source_path"]
            for solution in solutions
            if solution["expected_behavior"] == "compile_error"
        )
        selected = tuple(
            solution
            for solution in solutions
            if solution["source_path"] not in omitted
        )
        warning = (
            "ICPC 2025-09 omitted submissions authored as compile_error: "
            + ", ".join(omitted)
            if omitted
            else ""
        )
        return PackageAdapterPlan("icpc-2025-09", selected, warning)

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        placement: ContestPackagePlacement | None = None,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        del placement
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        self.prepare_target(target)

        snapshot = reader.root
        mode = reader.manifest["mode"]
        pass_limit = reader.manifest["pass_limit"]
        problem_name = statement_title_from_snapshot(
            snapshot,
            fallback_title=self.problem_slug_leaf(canonical_problem_slug),
        )
        checker, interactor, validator = self.package_programs(
            snapshot,
            load_build_config(snapshot),
            mode=mode,
        )
        self.hydrate_statement_samples(reader)
        statement_names = self.write_statements(
            snapshot,
            target / "statement",
            problem_name=problem_name,
            include_sample_tests=not self.samples_are_secret(mode, pass_limit),
            keep_all_languages=True,
        )
        problem_config = load_problem_config(
            snapshot,
            limits=problem_config_limits(self._config_values),
        )
        (target / "problem.yaml").write_text(
            render_problem_yaml(
                problem_slug=canonical_problem_slug,
                source_commit=reader.native_package["source_commit"],
                names=statement_names,
                mode=mode,
                pass_limit=pass_limit,
                time_limit_ms=problem_config["time_limit_ms"],
                memory_limit_mb=problem_config["memory_limit_mb"],
            ),
            encoding="utf-8",
            newline="\n",
        )
        self.copy_test_data(
            reader,
            target,
            samples_as_secret=self.samples_are_secret(mode, pass_limit),
        )
        self.write_validators(
            snapshot,
            target,
            validator=validator,
            output_validator=interactor if mode == "interactive" else checker,
        )
        submission_metadata = self.copy_submissions(
            snapshot,
            target,
            solutions=adapter_plan.solutions,
            collect_metadata=True,
            annotate_mixed=False,
        )
        (target / "submissions" / "submissions.yaml").write_text(
            render_submissions_yaml(submission_metadata),
            encoding="utf-8",
            newline="\n",
        )
        self.copy_attachments(snapshot, target)
        return adapter_plan.warning
