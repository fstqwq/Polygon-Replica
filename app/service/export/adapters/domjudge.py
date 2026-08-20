"""DOMjudge-compatible problem package adapter."""

import hashlib
import re
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


def render_domjudge_problem_yaml(
    *,
    names: dict[str, str],
    mode: str,
    pass_limit: int,
    time_limit_ms: int,
    memory_limit_mb: int | None,
) -> str:
    """Render the metadata shape consumed by supported DOMjudge releases."""

    if not names:
        raise ValueError("DOMjudge export requires at least one problem statement")
    name_value: str | dict[str, str]
    if "en" in names:
        name_value = names["en"]
    else:
        name_value = names[next(iter(names))]
    if mode == "interactive":
        validation = "custom interactive"
        legacy_type = (
            "pass-fail multi-pass" if pass_limit > 1 else "pass-fail"
        )
    elif pass_limit > 1:
        validation = "custom multi-pass"
        legacy_type = "pass-fail"
    else:
        validation = "custom"
        legacy_type = "pass-fail"
    limits: dict[str, object] = {
        "time_limit": max(0.001, time_limit_ms / 1000.0),
    }
    if memory_limit_mb is not None:
        limits["memory"] = memory_limit_mb
    if pass_limit > 1:
        limits["validation_passes"] = pass_limit
    payload: dict[str, object] = {
        "problem_format_version": "legacy",
        "type": legacy_type,
        "name": name_value,
        "validation": validation,
        "limits": limits,
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


class DOMjudgePackageAdapter(PackageAdapterSupport):
    """Write one DOMjudge-compatible problem package tree."""

    format: PackageFormat = "domjudge"
    display_name = "DOMjudge"

    COLOR_PALETTE = (
        "#e6194b",  # A Red
        "#4363d8",  # B Blue
        "#ffe119",  # C Yellow
        "#3cb44b",  # D Green
        "#f58231",  # E Orange
        "#6b2c91",  # F Purple
        "#eeeeee",  # G White
        "#9a6324",  # H Brown
        "#46d9e6",  # I Cyan
        "#303030",  # J Black
        "#ff6f91",  # K Pink
        "#9bdc28",  # L Lime
        "#9e9e9e",  # M Silver
        "#008080",  # N Teal
        "#d4a017",  # O Gold
        "#800000",  # P Burgundy
        "#aaffc3",  # Q Mint
        "#ffd8b1",  # R Peach
    )

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        super().__init__(config_values, tex_compile_service)

    @staticmethod
    def plan(reader: NativePackageReader) -> PackageAdapterPlan:
        return PackageAdapterPlan(
            "domjudge",
            tuple(reader.manifest["solutions"]),
            "",
        )

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        placement: ContestPackagePlacement | None = None,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        external_id = self.external_id(canonical_problem_slug)
        resolved_short_name = self.short_name(
            placement.idx if placement is not None else external_id
        )
        color = self.balloon_color(
            external_id=external_id,
            placement=placement,
        )
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
            target / "problem_statement",
            problem_name=problem_name,
            include_sample_tests=not self.samples_are_secret(mode, pass_limit),
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
            self.problem_ini(
                problem_name=problem_name,
                external_id=external_id,
                short_name=resolved_short_name,
                time_limit_ms=problem_config["time_limit_ms"],
                color=color,
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
        self.copy_submissions(
            snapshot,
            target,
            solutions=adapter_plan.solutions,
            collect_metadata=False,
            annotate_mixed=True,
        )
        self.copy_attachments(snapshot, target)
        return adapter_plan.warning

    @staticmethod
    def short_name(value: str | None) -> str:
        if value is None or not value:
            raise ValueError("DOMjudge short-name is required")
        if "\n" in value or "\r" in value:
            raise ValueError("DOMjudge short-name must be a single line")
        return value

    @classmethod
    def external_id(cls, canonical_problem_slug: str) -> str:
        token = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            cls.problem_slug_leaf(canonical_problem_slug),
        ).strip("-.")
        return token or "problem"

    @staticmethod
    def ini_value(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9 .,_/-]+", value):
            return value
        escaped = value.replace("\\", "\\\\").replace('"', r'\"')
        return f'"{escaped}"'

    @classmethod
    def balloon_color(
        cls,
        *,
        external_id: str,
        placement: ContestPackagePlacement | None,
    ) -> str:
        if placement is not None:
            return cls.COLOR_PALETTE[
                (placement.ordinal - 1) % len(cls.COLOR_PALETTE)
            ]
        digest = hashlib.sha256(external_id.encode("utf-8")).digest()
        return cls.COLOR_PALETTE[digest[0] % len(cls.COLOR_PALETTE)]

    @classmethod
    def problem_ini(
        cls,
        *,
        problem_name: str,
        external_id: str,
        short_name: str,
        time_limit_ms: int,
        color: str,
    ) -> str:
        seconds = time_limit_ms / 1000.0
        return (
            f"name = {cls.ini_value(problem_name)}\n"
            f"externalid = {cls.ini_value(external_id)}\n"
            f"short-name = {cls.ini_value(short_name)}\n"
            f"timelimit = {seconds:.3f}".rstrip("0").rstrip(".")
            + "\n"
            f"color = {color}\n"
        )
