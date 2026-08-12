from __future__ import annotations

import json
import shlex
from pathlib import Path

from app.impl.runtime.config import config
from app.service.platform.testlib_source import workspace_testlib_header
from app.service.platform.runtime_blob_store import PayloadFile
from app.service.problem.build_config import BuildConfig
from app.service.problem.runtime_config import ProblemConfig, problem_config_limits
from app.service.problem.source_tree import load_problem_source_tree
from app.service.problem.test_spec import TestSpecEntry
from app.service.verification.service import CPP_EXTENSIONS, SOLUTION_SOURCE_EXTENSIONS
from app.service.verification.plan import VerificationExecutionPlan, VerificationTestPlan
from app.service.verification.signature import VerificationManifest, verification_manifest
from app.service.verification.source import resolve_source


def _problem_limits(runtime_cfg: ProblemConfig) -> dict[str, int]:
    return {
        "time_limit_ms": runtime_cfg["time_limit_ms"],
        "memory_limit_mb": runtime_cfg["memory_limit_mb"],
        "pass_limit": runtime_cfg["pass_limit"],
    }


def _run_payload_base(
    *,
    build_cfg: BuildConfig,
    problem_limits: dict[str, int],
    source_files: dict[str, PayloadFile],
) -> dict[str, object]:
    checker_args = list(build_cfg["checker_args"])
    return {
        "run_config_json": json.dumps(
            {
                "checker_mode": "testlib",
                "checker_args": checker_args,
                "pass_limit": int(problem_limits["pass_limit"]),
                "time_limit_ms": int(problem_limits["time_limit_ms"]),
                "memory_limit_mb": int(problem_limits["memory_limit_mb"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "problem_limits": dict(problem_limits),
        "source_files": {name: payload.to_payload() for name, payload in source_files.items()},
    }


def _generate_payload_base(
    *,
    problem_limits: dict[str, int],
    source_files: dict[str, PayloadFile],
) -> dict[str, object]:
    return {
        "run_config_json": json.dumps(
            {
                "checker_mode": "testlib",
                "checker_args": [],
                "pass_limit": int(problem_limits["pass_limit"]),
                "time_limit_ms": 30000,
                "memory_limit_mb": int(problem_limits["memory_limit_mb"]),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "problem_limits": {
            "time_limit_ms": 30000,
            "memory_limit_mb": int(problem_limits["memory_limit_mb"]),
            "pass_limit": int(problem_limits["pass_limit"]),
        },
        "source_files": {name: payload.to_payload() for name, payload in source_files.items()},
    }


def _shared_source_payloads(
    *,
    snapshot: Path,
    manifest: VerificationManifest,
    build_cfg: BuildConfig,
    mode: str,
) -> dict[str, object]:
    snapshot_resolved = snapshot.resolve()
    verification_service = config.verification_service
    validator_source = verification_service._select_source(
        snapshot,
        build_cfg,
        "validator_source",
        "validators",
        snapshot_resolved=snapshot_resolved,
    )
    if mode == "interactive":
        checker_source = None
        interactor_source = verification_service._select_source(
            snapshot,
            build_cfg,
            "interactor_source",
            "interactors",
            snapshot_resolved=snapshot_resolved,
        )
        if interactor_source is None:
            raise RuntimeError("interactor source is required for interactive mode")
    else:
        checker_source = verification_service._select_checker_source(
            snapshot,
            build_cfg,
            snapshot_resolved=snapshot_resolved,
        )
        interactor_source = None
    accepted_source_path = build_cfg.get("accepted_solution_source", "")
    if not accepted_source_path:
        raise RuntimeError("accepted solution is missing")
    if not accepted_source_path.startswith("solutions/"):
        raise RuntimeError("accepted solution source must be under solutions/")
    if Path(accepted_source_path).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
        raise RuntimeError("accepted solution source must be .cpp/.cc/.cxx/.c++/.py/.java")
    manifest.require(accepted_source_path)
    source_file_by_path = {
        accepted_source_path: manifest.require(accepted_source_path),
    }
    testlib_header = workspace_testlib_header(snapshot)
    source_files: dict[str, PayloadFile] = {}
    if checker_source is not None:
        source_files["checker.cpp"] = manifest.require(checker_source.relative_to(snapshot).as_posix())
    if validator_source is not None:
        source_files["validator.cpp"] = manifest.require(validator_source.relative_to(snapshot).as_posix())
    if interactor_source is not None:
        source_files["interactor.cpp"] = manifest.require(interactor_source.relative_to(snapshot).as_posix())
    if testlib_header is not None:
        source_files["testlib.h"] = manifest.require(testlib_header.relative_to(snapshot).as_posix())
    return {
        "accepted_source_path": accepted_source_path,
        "source_file_by_path": source_file_by_path,
        "source_files": source_files,
        "testlib_header": testlib_header,
    }


def _generator_command_payload(args: list[str]) -> str:
    if not args:
        return "\"$SUBMISSION_BIN\""
    return " ".join(["\"$SUBMISSION_BIN\"", *[shlex.quote(item) for item in args]])


def _manual_plan(
    *,
    test_name: str,
    input_bytes: bytes | None = None,
    input_file: PayloadFile | None = None,
    tests_meta: dict[str, object],
    sample: bool = False,
    sample_input_custom: bool = False,
    sample_input_text: str = "",
    uses_custom_sample_input: bool = False,
    sample_output_text: str = "",
    sample_output_validate: bool = True,
) -> VerificationTestPlan:
    execution_input = input_file
    if execution_input is None:
        execution_input = config.runtime_blob_store.put_bytes(input_bytes or b"")
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="manual",
        display_source_path="manual_validate.cpp",
        execution_source_name="manual_validate.cpp",
        execution_source_file=config.runtime_blob_store.put_bytes(b"int main(){return 0;}\n"),
        execution_input_file=execution_input,
        extra_source_files={},
        tests_meta=tests_meta,
        sample=sample,
        sample_input_custom=sample_input_custom,
        sample_input_text=sample_input_text,
        uses_custom_sample_input=uses_custom_sample_input,
        sample_output_text=sample_output_text,
        sample_output_validate=sample_output_validate,
    )


def _generated_plan(
    *,
    snapshot: Path,
    manifest: VerificationManifest,
    test_name: str,
    display_source_path: str,
    generator_source: Path,
    command_payload: str,
    tests_meta: dict[str, object],
    testlib_header: Path | None,
    sample: bool = False,
    sample_input_custom: bool = False,
    sample_input_text: str = "",
    uses_custom_sample_input: bool = False,
    sample_output_text: str = "",
    sample_output_validate: bool = True,
) -> VerificationTestPlan:
    extra_source_files: dict[str, PayloadFile] = {}
    if generator_source.suffix.lower() in CPP_EXTENSIONS and testlib_header is not None:
        extra_source_files["testlib.h"] = manifest.require(
            testlib_header.relative_to(snapshot).as_posix()
        )
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="gen",
        display_source_path=display_source_path,
        execution_source_name=generator_source.name,
        execution_source_file=manifest.require(generator_source.relative_to(snapshot).as_posix()),
        execution_input_file=config.runtime_blob_store.put_bytes((command_payload + "\n").encode("utf-8")),
        extra_source_files=extra_source_files,
        tests_meta=tests_meta,
        sample=sample,
        sample_input_custom=sample_input_custom,
        sample_input_text=sample_input_text,
        uses_custom_sample_input=uses_custom_sample_input,
        sample_output_text=sample_output_text,
        sample_output_validate=sample_output_validate,
    )


def _tests_from_spec(
    *,
    snapshot: Path,
    manifest: VerificationManifest,
    testlib_header: Path | None,
    sample_only: bool,
    build_cfg: BuildConfig,
    entries: tuple[TestSpecEntry, ...],
) -> tuple[list[VerificationTestPlan], list[dict[str, object]]]:
    verification_service = config.verification_service
    runtime_rows, generator_targets = verification_service._prepare_tests_spec_runtime(
        snapshot,
        list(entries),
        generator_sources=build_cfg["generator_sources"],
    )
    generator_source_by_name = {
        str(target_name): source_path
        for target_name, source_path in generator_targets
        if source_path is not None
    }
    plans: list[VerificationTestPlan] = []
    tests_meta_rows: list[dict[str, object]] = []
    counter = 1
    for row in runtime_rows:
        test_id = str(row["id"])
        sample = bool(row["sample"])
        sample_input = str(row["sample_input"])
        sample_output = str(row["sample_output"])
        sample_output_validate = bool(row["sample_output_validate"])
        use_custom_sample_input = sample_only and sample and bool(sample_input)
        test_name = f"{counter:03d}.in"
        if str(row["kind"]) == "manual" or use_custom_sample_input:
            tests_meta = {
                "index": counter,
                "test_name": test_name,
                "kind": str(row["kind"]),
                "id": test_id,
                "sample": sample,
                "sample_input_custom": bool(sample_input),
                "sample_output_custom": bool(sample_output),
                "sample_output_validate": sample_output_validate,
                "desc": f"{str(row['kind'])} {test_id}" if test_id else str(row["kind"]),
                "source": str(row["source_rel"]),
            }
            plan = _manual_plan(
                test_name=test_name,
                input_bytes=sample_input.encode("utf-8") if use_custom_sample_input else None,
                input_file=None if use_custom_sample_input else manifest.require(str(row["source_rel"])),
                tests_meta=tests_meta,
                sample=sample,
                sample_input_custom=bool(sample_input),
                sample_input_text=sample_input,
                uses_custom_sample_input=use_custom_sample_input,
                sample_output_text=sample_output,
                sample_output_validate=sample_output_validate,
            )
        else:
            target_name = str(row["target_name"])
            generator_source = generator_source_by_name.get(target_name)
            if generator_source is None:
                raise RuntimeError(f"generator source is required for tests/spec.json entry {row['index']}")
            tests_meta = {
                "index": counter,
                "test_name": test_name,
                "kind": "gen",
                "id": test_id,
                "sample": sample,
                "sample_input_custom": bool(sample_input),
                "sample_output_custom": bool(sample_output),
                "sample_output_validate": sample_output_validate,
                "desc": str(row["cmd"]) if str(row["cmd"]) else "gen",
                "command": str(row["cmd"]),
                "source": str(row["source_rel"]),
                "payload_source": str(row["payload_rel"]),
            }
            plan = _generated_plan(
                snapshot=snapshot,
                manifest=manifest,
                test_name=test_name,
                display_source_path=str(row["source_rel"]),
                generator_source=generator_source,
                command_payload=_generator_command_payload([str(item) for item in row["args"]]),
                tests_meta=tests_meta,
                testlib_header=testlib_header,
                sample=sample,
                sample_input_custom=bool(sample_input),
                sample_input_text=sample_input,
                uses_custom_sample_input=False,
                sample_output_text=sample_output,
                sample_output_validate=sample_output_validate,
            )
        plans.append(plan)
        tests_meta_rows.append(dict(plan.tests_meta))
        counter += 1
    return (plans, tests_meta_rows)


def _tests_without_spec(
    *,
    snapshot: Path,
    manifest: VerificationManifest,
    build_cfg: BuildConfig,
    testlib_header: Path | None,
) -> tuple[list[VerificationTestPlan], list[dict[str, object]]]:
    verification_service = config.verification_service
    snapshot_resolved = snapshot.resolve()
    plans: list[VerificationTestPlan] = []
    tests_meta_rows: list[dict[str, object]] = []
    counter = 1
    for manual_source in verification_service._manual_test_sources(snapshot):
        try:
            source_rel = manual_source.relative_to(snapshot).as_posix()
        except ValueError:
            source_rel = manual_source.name
        tests_meta = {
            "index": counter,
            "test_name": f"{counter:03d}.in",
            "kind": "manual",
            "desc": f"manual: {source_rel}",
            "source": source_rel,
        }
        plan = _manual_plan(
            test_name=f"{counter:03d}.in",
            input_file=manifest.require(manual_source.relative_to(snapshot).as_posix()),
            tests_meta=tests_meta,
        )
        plans.append(plan)
        tests_meta_rows.append(dict(tests_meta))
        counter += 1
    configured_generators = build_cfg["generator_sources"]
    generator_sources: list[Path] = []
    for rel in configured_generators:
        generator_sources.append(resolve_source(snapshot, str(rel), snapshot_resolved=snapshot_resolved))
    generator_args = build_cfg["generator_args"]
    generator_runs = build_cfg["generator_runs"]
    for source_path in generator_sources:
        try:
            source_label = source_path.relative_to(snapshot).as_posix()
        except ValueError:
            source_label = source_path.as_posix()
        for _ in range(generator_runs):
            tests_meta = {
                "index": counter,
                "test_name": f"{counter:03d}.in",
                "kind": "gen",
                "desc": f"gen: {source_label}" if not generator_args else f"gen: {source_label} {' '.join(generator_args)}",
                "source": source_label,
            }
            plan = _generated_plan(
                snapshot=snapshot,
                manifest=manifest,
                test_name=f"{counter:03d}.in",
                display_source_path=source_label,
                generator_source=source_path,
                command_payload=_generator_command_payload(generator_args),
                tests_meta=tests_meta,
                testlib_header=testlib_header,
            )
            plans.append(plan)
            tests_meta_rows.append(dict(tests_meta))
            counter += 1
    return (plans, tests_meta_rows)


def build_verification_execution_plan(
    snapshot: Path,
    *,
    manifest: VerificationManifest | None = None,
    sample_only: bool = False,
) -> VerificationExecutionPlan:
    resolved_manifest = verification_manifest(snapshot) if manifest is None else manifest
    verification_service = config.verification_service
    limits = config.config_values.snapshot()
    source_tree = load_problem_source_tree(
        snapshot,
        problem_limits=problem_config_limits(config.config_values),
        tests_spec_max_bytes=int(limits["TEXTAREA_MAX_BYTES"]),
        statement_sample_max_bytes=int(
            limits["STATEMENT_SAMPLE_MAX_BYTES"]
        ),
    )
    build_cfg = source_tree.build
    runtime_cfg = source_tree.problem
    mode = runtime_cfg["mode"]
    pass_limit = runtime_cfg["pass_limit"]
    shared_sources = _shared_source_payloads(
        snapshot=snapshot,
        manifest=resolved_manifest,
        build_cfg=build_cfg,
        mode=mode,
    )
    problem_limits = _problem_limits(runtime_cfg)
    plans, tests_meta_rows = _tests_from_spec(
        snapshot=snapshot,
        manifest=resolved_manifest,
        testlib_header=shared_sources["testlib_header"],
        sample_only=bool(sample_only),
        build_cfg=build_cfg,
        entries=source_tree.tests,
    )
    if not plans:
        plans, tests_meta_rows = _tests_without_spec(
            snapshot=snapshot,
            manifest=resolved_manifest,
            build_cfg=build_cfg,
            testlib_header=shared_sources["testlib_header"],
        )
    if sample_only:
        filtered_pairs = [
            (plan, meta)
            for plan, meta in zip(plans, tests_meta_rows, strict=False)
            if bool(meta.get("sample"))
        ]
        plans = [plan for plan, _meta in filtered_pairs]
        tests_meta_rows = [meta for _plan, meta in filtered_pairs]
    if not plans:
        raise RuntimeError("verification build produced no tests")
    test_plan_by_name = {plan.test_name: plan for plan in plans}
    return VerificationExecutionPlan(
        snapshot_root=snapshot,
        accepted_source_path=str(shared_sources["accepted_source_path"]),
        mode=mode,
        pass_limit=pass_limit,
        run_verification_payload_base=_run_payload_base(
            build_cfg=build_cfg,
            problem_limits=problem_limits,
            source_files=dict(shared_sources["source_files"]),
        ),
        generate_verification_payload_base=_generate_payload_base(
            problem_limits=problem_limits,
            source_files=dict(shared_sources["source_files"]),
        ),
        source_file_by_path=dict(shared_sources["source_file_by_path"]),
        test_names=[plan.test_name for plan in plans],
        test_plan_by_name=test_plan_by_name,
        tests_meta_rows=tests_meta_rows,
    )
