from __future__ import annotations

from app.service.verification.types import Kind, Status


VerificationSummary = dict[str, object]
VerificationRunRow = dict[str, object]


def _summary_runs(summary: VerificationSummary) -> dict[str, VerificationRunRow]:
    raw = summary.get("runs")
    return {} if not isinstance(raw, dict) else {str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)}


def _summary_run_order(summary: VerificationSummary) -> list[str]:
    raw = summary.get("runs_order")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str)]


def _summary_stage_results(summary: VerificationSummary) -> dict[str, VerificationSummary]:
    raw = summary.get("stage_results")
    if not isinstance(raw, dict):
        return {}
    return {str(key): dict(value) for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict)}


def _run_summary(run_row: VerificationRunRow) -> VerificationSummary:
    raw = run_row.get("summary")
    return {} if not isinstance(raw, dict) else dict(raw)


def _canonical_verification_step_id(raw: object) -> str:
    if isinstance(raw, str):
        token = raw
    elif isinstance(raw, dict):
        step_obj = raw.get("step") or raw.get("id") or raw.get("step_id") or ""
        if not isinstance(step_obj, str):
            return ""
        token = step_obj
    else:
        return ""
    step_id = token.strip().lower().replace("-", "_")
    if step_id in {"compile", "generate", "gen", "generate_input"}:
        return "gen"
    if step_id in {"validate", "val", "generate_output", "generate_outputs"}:
        return "val"
    if step_id in {"solve", "run"}:
        return "run"
    if step_id in {"check", "check_expectations"}:
        return "check"
    return ""


def _sanitize_verification_run_summary(run_summary: VerificationSummary) -> VerificationSummary:
    payload = dict(run_summary)
    for legacy_key in (
        "run_id",
        "run_ids",
        "run_count",
        "artifact_verification_status",
        "artifact_verification_error",
        "artifact_failed_step",
        "artifact_failed_test",
        "build_id",
        "build_ref",
        "verification_kind",
        "invocation",
        "invocation_id",
        "invocation_source",
    ):
        payload.pop(legacy_key, None)
    return payload


def _sanitize_verification_summary(summary: VerificationSummary) -> VerificationSummary:
    payload = dict(summary)
    for legacy_key in (
        "run_id",
        "run_ids",
        "run_count",
        "build_id",
        "build_ref",
        "verification_kind",
        "invocation",
        "invocation_id",
        "invocation_source",
    ):
        payload.pop(legacy_key, None)
    sanitized_runs: dict[str, VerificationRunRow] = {}
    for key, item in _summary_runs(payload).items():
        run_row = dict(item)
        run_row["run_id"] = key
        run_row.pop("key", None)
        run_summary = run_row.get("summary")
        if isinstance(run_summary, dict):
            run_row["summary"] = _sanitize_verification_run_summary(run_summary)
        sanitized_runs[key] = run_row
    payload["runs"] = sanitized_runs
    sanitized_steps: list[str] = []
    raw_steps = payload.get("steps")
    if isinstance(raw_steps, list):
        for raw_step in raw_steps:
            step_id = _canonical_verification_step_id(raw_step)
            if step_id and step_id not in sanitized_steps:
                sanitized_steps.append(step_id)
    if sanitized_steps:
        payload["steps"] = sanitized_steps
    else:
        payload.pop("steps", None)
    sanitized_order = _summary_run_order(payload)
    for token in sanitized_runs.keys():
        if token not in sanitized_order:
            sanitized_order.append(token)
    payload["runs_order"] = sanitized_order
    stage_results = verification_stage_results(payload)
    if stage_results:
        payload["stage_results"] = stage_results
    else:
        payload.pop("stage_results", None)
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, dict):
        steps_raw = lifecycle.get("steps")
        if isinstance(steps_raw, list):
            sanitized_lifecycle_steps: list[object] = []
            for item in steps_raw:
                if isinstance(item, dict):
                    step_copy = dict(item)
                    step_id = _canonical_verification_step_id(item)
                    if step_id:
                        step_copy["step"] = step_id
                    sanitized_lifecycle_steps.append(step_copy)
                    continue
                step_id = _canonical_verification_step_id(item)
                if step_id:
                    sanitized_lifecycle_steps.append(step_id)
            lifecycle["steps"] = sanitized_lifecycle_steps
            payload["lifecycle"] = lifecycle
    return payload


def default_verification_summary(
    *,
    kind: str,
    mode: str,
    source_commit: str = "",
    source_ref: str = "",
    source_paths: list[str] | None = None,
    verification_source: str = "",
) -> VerificationSummary:
    return {
        "kind": kind or Kind.VERIFICATION.value,
        "mode": mode or "pass-fail",
        "source_commit": source_commit,
        "source_ref": source_ref,
        "status": Status.RUNNING.value,
        "source_paths": list(source_paths or []),
        "verification_source": verification_source or "run.execute",
        "error": "",
        "updated_at": "",
        "finished_at": "",
        "artifact_root": "",
        "runs_order": [],
        "runs": {},
        "tests": [],
        "lifecycle": {"steps": []},
    }


def default_verification_run(
    *,
    run_id: str,
    source_label: str,
    expected_behavior: str,
    run_status: str = Status.RUNNING.value,
    artifact_path: str = "",
    task_kind: str = "",
) -> VerificationRunRow:
    if not run_id:
        raise RuntimeError("run id is required")
    return {
        "run_id": run_id,
        "status": run_status or Status.RUNNING.value,
        "source_label": source_label or run_id,
        "expected_behavior": expected_behavior or "unknown",
        "artifact_path": artifact_path,
        "task_kind": task_kind,
        "summary": {},
    }


def verification_run_ids(summary: VerificationSummary) -> list[str]:
    values: list[str] = []
    for token in _summary_run_order(summary):
        if token and token not in values:
            values.append(token)
    for token in _summary_runs(summary).keys():
        if token and token not in values:
            values.append(token)
    return values


def _is_solution_source_path(value: str) -> bool:
    token = value.replace("\\", "/").lstrip("./")
    return bool(token) and token.startswith("solutions/")


def verification_source_paths(summary: VerificationSummary) -> list[str]:
    values: list[str] = []
    for key in ("source_paths", "submission_paths"):
        raw_paths = summary.get(key)
        if not isinstance(raw_paths, list):
            continue
        for token in raw_paths:
            if isinstance(token, str) and token and _is_solution_source_path(token) and token not in values:
                values.append(token)
    for item in _summary_runs(summary).values():
        source_label = item.get("source_label")
        if isinstance(source_label, str) and _is_solution_source_path(source_label) and source_label not in values:
            values.append(source_label)
            continue
        source = _run_summary(item).get("source")
        if isinstance(source, str) and _is_solution_source_path(source) and source not in values:
            values.append(source)
    return values


def verification_stage_results(summary: VerificationSummary) -> dict[str, VerificationSummary]:
    sanitized: dict[str, VerificationSummary] = {}
    for key, item in _summary_stage_results(summary).items():
        if key:
            sanitized[key] = _sanitize_verification_run_summary(item)
    return sanitized


def verification_stage_summary(summary: VerificationSummary, stage_key: str) -> VerificationSummary:
    return dict(verification_stage_results(summary).get(stage_key) or {})


def verification_run(summary: VerificationSummary, run_id: str) -> VerificationRunRow:
    if not run_id:
        return {}
    return dict(_summary_runs(summary).get(run_id) or {})


def sanitize_verification_summary(summary: VerificationSummary) -> VerificationSummary:
    return _sanitize_verification_summary(summary)


def sanitize_verification_run_summary(summary: VerificationSummary) -> VerificationSummary:
    return _sanitize_verification_run_summary(summary)
