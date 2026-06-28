from __future__ import annotations

import base64
import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from app.impl.runtime.config import config
from app.service.platform.testlib_source import workspace_testlib_header
from app.service.verification.service import CPP_EXTENSIONS, DEFAULT_TIME_LIMIT_MS, SOLUTION_SOURCE_EXTENSIONS
from app.service.verification.runtime import normalize_pass_limit, normalize_problem_mode
from app.service.verification.source import resolve_source
from .context_operation import list_solution_entries, resolve_build_accepted_solution_source


@dataclass(frozen=True)
class VerificationTestPlan:
    test_name: str
    source_kind: str
    display_source_path: str
    execution_source_name: str
    execution_source_bytes: bytes
    execution_input_bytes: bytes
    extra_sources_b64: dict[str, str]
    tests_meta: dict[str, object]
    sample: bool
    sample_input_custom: bool
    sample_input_text: str
    uses_custom_sample_input: bool
    sample_output_text: str
    sample_output_validate: bool


@dataclass(frozen=True)
class VerificationExecutionPlan:
    snapshot_root: Path
    accepted_source_path: str
    mode: str
    pass_limit: int
    run_verification_payload_base: dict[str, object]
    generate_verification_payload_base: dict[str, object]
    source_file_by_path: dict[str, Path]
    test_names: list[str]
    test_plan_by_name: dict[str, VerificationTestPlan]
    tests_meta_rows: list[dict[str, object]]


def _problem_limits(runtime_cfg: dict[str, object], *, pass_limit: int) -> dict[str, int]:
    try:
        time_limit_ms = int(runtime_cfg.get("time_limit_ms", DEFAULT_TIME_LIMIT_MS))
    except Exception:
        time_limit_ms = DEFAULT_TIME_LIMIT_MS
    try:
        memory_limit_mb = int(runtime_cfg.get("memory_limit_mb", 1024))
    except Exception:
        memory_limit_mb = 1024
    return {
        "time_limit_ms": max(100, time_limit_ms),
        "memory_limit_mb": max(16, memory_limit_mb),
        "pass_limit": max(1, int(pass_limit)),
    }


def _run_payload_base(
    *,
    build_cfg: dict[str, object],
    problem_limits: dict[str, int],
    source_payloads_b64: dict[str, str],
) -> dict[str, object]:
    checker_args_raw = build_cfg.get("checker_args") or []
    checker_args = [str(item) for item in checker_args_raw if str(item or "")]
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
        "binaries_b64": {},
        "sources_b64": dict(source_payloads_b64),
    }


def _generate_payload_base(
    *,
    problem_limits: dict[str, int],
    source_payloads_b64: dict[str, str],
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
        "binaries_b64": {},
        "sources_b64": dict(source_payloads_b64),
    }


def _optional_b64(path: Path | None) -> str:
    if path is None:
        return ""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _shared_source_payloads(
    *,
    snapshot: Path,
    build_cfg: dict[str, object],
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
    checker_source = (
        None
        if mode == "interactive"
        else verification_service._select_checker_source(
            snapshot,
            build_cfg,
            snapshot_resolved=snapshot_resolved,
        )
    )
    interactor_source = verification_service._select_source(
        snapshot,
        build_cfg,
        "interactor_source",
        "interactors",
        snapshot_resolved=snapshot_resolved,
    )
    accepted_source_path = resolve_build_accepted_solution_source(snapshot, list_solution_entries(snapshot)[0]) or ""
    if not accepted_source_path:
        raise RuntimeError("accepted solution is missing")
    if not accepted_source_path.startswith("solutions/"):
        raise RuntimeError("accepted solution source must be under solutions/")
    if Path(accepted_source_path).suffix.lower() not in SOLUTION_SOURCE_EXTENSIONS:
        raise RuntimeError("accepted solution source must be .cpp/.cc/.cxx/.c++/.py/.java")
    accepted_source = resolve_source(snapshot, accepted_source_path, snapshot_resolved=snapshot_resolved)
    if mode == "interactive" and interactor_source is None:
        raise RuntimeError("interactor source is required for interactive mode")
    source_file_by_path = {
        accepted_source_path: accepted_source,
    }
    testlib_header = workspace_testlib_header(snapshot)
    sources_b64: dict[str, str] = {}
    if checker_source is not None:
        sources_b64["checker.cpp"] = _optional_b64(checker_source)
    if validator_source is not None:
        sources_b64["validator.cpp"] = _optional_b64(validator_source)
    if interactor_source is not None:
        sources_b64["interactor.cpp"] = _optional_b64(interactor_source)
    if testlib_header is not None:
        sources_b64["testlib.h"] = base64.b64encode(testlib_header.read_bytes()).decode("ascii")
    return {
        "accepted_source_path": accepted_source_path,
        "source_file_by_path": source_file_by_path,
        "sources_b64": sources_b64,
        "testlib_header": testlib_header,
    }


def _generator_command_payload(args: list[str]) -> str:
    if not args:
        return "\"$SUBMISSION_BIN\""
    return " ".join(["\"$SUBMISSION_BIN\"", *[shlex.quote(item) for item in args]])


def _manual_plan(
    *,
    test_name: str,
    input_bytes: bytes,
    tests_meta: dict[str, object],
    sample: bool = False,
    sample_input_custom: bool = False,
    sample_input_text: str = "",
    uses_custom_sample_input: bool = False,
    sample_output_text: str = "",
    sample_output_validate: bool = True,
) -> VerificationTestPlan:
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="manual",
        display_source_path="manual_validate.cpp",
        execution_source_name="manual_validate.cpp",
        execution_source_bytes=b"int main(){return 0;}\n",
        execution_input_bytes=input_bytes,
        extra_sources_b64={},
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
    extra_sources_b64: dict[str, str] = {}
    if generator_source.suffix.lower() in CPP_EXTENSIONS and testlib_header is not None:
        extra_sources_b64["testlib.h"] = base64.b64encode(testlib_header.read_bytes()).decode("ascii")
    return VerificationTestPlan(
        test_name=test_name,
        source_kind="gen",
        display_source_path=display_source_path,
        execution_source_name=generator_source.name,
        execution_source_bytes=generator_source.read_bytes(),
        execution_input_bytes=(command_payload + "\n").encode("utf-8"),
        extra_sources_b64=extra_sources_b64,
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
    bin_dir: Path,
    testlib_header: Path | None,
    sample_only: bool,
) -> tuple[list[VerificationTestPlan], list[dict[str, object]]]:
    verification_service = config.verification_service
    entries = verification_service._load_tests_spec(snapshot)
    if entries is None:
        return ([], [])
    runtime_rows, generator_targets = verification_service._prepare_tests_spec_runtime(snapshot, entries, bin_dir)
    generator_source_by_name = {
        str(target_name): source_path
        for target_name, source_path, _target_bin in generator_targets
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
                input_bytes=sample_input.encode("utf-8") if use_custom_sample_input else str(row["input"]).encode("utf-8"),
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
    build_cfg: dict[str, object],
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
            input_bytes=manual_source.read_bytes(),
            tests_meta=tests_meta,
        )
        plans.append(plan)
        tests_meta_rows.append(dict(tests_meta))
        counter += 1
    configured_generators = list(build_cfg.get("generator_sources") or [])
    generator_sources: list[Path] = []
    for rel in configured_generators:
        generator_sources.append(resolve_source(snapshot, str(rel), snapshot_resolved=snapshot_resolved))
    generator_args = [str(item) for item in list(build_cfg.get("generator_args") or [])]
    generator_runs = max(0, int(build_cfg.get("generator_runs", 3) or 0))
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
    bin_dir: Path,
    sample_only: bool = False,
) -> VerificationExecutionPlan:
    verification_service = config.verification_service
    build_cfg = verification_service._load_build_config(snapshot)
    runtime_cfg = verification_service._load_problem_runtime_config(snapshot)
    mode = normalize_problem_mode(runtime_cfg.get("mode"), "pass-fail")
    pass_limit = normalize_pass_limit(runtime_cfg.get("pass_limit"), 1)
    shared_sources = _shared_source_payloads(snapshot=snapshot, build_cfg=build_cfg, mode=mode)
    problem_limits = _problem_limits(runtime_cfg, pass_limit=pass_limit)
    plans, tests_meta_rows = _tests_from_spec(
        snapshot=snapshot,
        bin_dir=bin_dir,
        testlib_header=shared_sources["testlib_header"],
        sample_only=bool(sample_only),
    )
    if not plans:
        plans, tests_meta_rows = _tests_without_spec(
            snapshot=snapshot,
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
            source_payloads_b64=dict(shared_sources["sources_b64"]),
        ),
        generate_verification_payload_base=_generate_payload_base(
            problem_limits=problem_limits,
            source_payloads_b64=dict(shared_sources["sources_b64"]),
        ),
        source_file_by_path=dict(shared_sources["source_file_by_path"]),
        test_names=[plan.test_name for plan in plans],
        test_plan_by_name=test_plan_by_name,
        tests_meta_rows=tests_meta_rows,
    )
