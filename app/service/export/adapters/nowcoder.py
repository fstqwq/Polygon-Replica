"""Nowcoder single-pass pass-fail package adapter."""

import shutil
from pathlib import Path

from app.service.export.adapters.shared import (
    PackageAdapterPlan,
    PackageAdapterSupport,
    PackageFormat,
)
from app.service.problem.build_config import load_build_config
from app.service.problem_package.service import NativePackageReader


class NowcoderPackageAdapter:
    """Write the flat testcase layout accepted by Nowcoder."""

    format: PackageFormat = "nowcoder"
    display_name = "Nowcoder"
    accepts_short_name = False

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
        short_name: str | None = None,
        plan: PackageAdapterPlan | None = None,
    ) -> str:
        del canonical_problem_slug
        if short_name is not None:
            raise ValueError("Nowcoder package does not accept a short-name")
        adapter_plan = plan or self.plan(reader)
        if adapter_plan.package_format != self.format:
            raise ValueError("package adapter plan format does not match request")
        self._require_supported_problem(reader)
        PackageAdapterSupport.prepare_target(target)

        for number, test in enumerate(reader.manifest["tests"], start=1):
            test_id = test["id"]
            input_source = reader.payload(test, "input")
            if input_source is None:
                raise ValueError(f"Native Package test input is missing: {test_id}")
            answer_source = reader.payload(test, "answer")
            if answer_source is None:
                raise ValueError(f"Native Package test answer is missing: {test_id}")
            shutil.copy2(input_source, target / f"{number}.in")
            shutil.copy2(answer_source, target / f"{number}.ans")

        checker = self._checker(reader)
        if checker is not None:
            shutil.copy2(checker, target / "checker.cc")
        return adapter_plan.warning

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
        build_config = load_build_config(
            reader.root,
            problem_mode="pass-fail",
        )
        return PackageAdapterSupport.configured_source(
            reader.root,
            build_config.get("checker_source"),
        )
