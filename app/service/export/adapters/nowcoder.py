"""Nowcoder single-pass pass-fail package adapter."""

import shutil
from pathlib import Path
from typing import cast

from app.service.export.adapters.shared import (
    ContestPackagePlacement,
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem_package.service import NativePackageReader


class NowcoderPackageAdapter:
    """Write the flat testcase layout accepted by Nowcoder."""

    format: PackageFormat = "nowcoder"
    display_name = "Nowcoder"

    def plan(self, reader: NativePackageReader) -> PackageAdapterPlan:
        self._require_supported_problem(reader)
        checker = self._checker(reader)
        warning = ""
        if checker is not None and b"setTestCase" in checker.read_bytes():
            warning = (
                "Nowcoder checker contains setTestCase, which the older "
                "Nowcoder testlib may not support"
            )
        return PackageAdapterPlan(self.format, (), warning)

    def build(
        self,
        reader: NativePackageReader,
        *,
        target: Path,
        canonical_problem_slug: str,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        del canonical_problem_slug
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        self._require_supported_problem(reader)
        PackageAdapterSupport.prepare_target(target)

        for number, test in enumerate(reader.manifest["tests"], start=1):
            input_source = cast(Path, reader.payload(test, "input"))
            answer_source = cast(Path, reader.payload(test, "answer"))
            shutil.copy2(input_source, target / f"{number}.in")
            shutil.copy2(answer_source, target / f"{number}.ans")

        checker = self._checker(reader)
        if checker is not None:
            shutil.copy2(checker, target / "checker.cc")
        return adapter_plan.warning

    @staticmethod
    def apply_contest_placement(
        target: Path,
        *,
        canonical_problem_slug: str,
        placement: ContestPackagePlacement,
    ) -> None:
        del target, canonical_problem_slug, placement

    @staticmethod
    def _require_supported_problem(reader: NativePackageReader) -> None:
        if (
            reader.manifest["mode"] != "pass-fail"
            or reader.manifest["pass_limit"] != 1
        ):
            raise ValueError(
                "Nowcoder package supports only single-pass pass-fail problems"
            )

    def _checker(self, reader: NativePackageReader) -> Path | None:
        build_config = PackageAdapterSupport.native_build_config(
            reader.root,
            problem_mode="pass-fail",
        )
        return PackageAdapterSupport.configured_source(
            reader.root,
            build_config.get("checker_source"),
        )
