from __future__ import annotations

from pathlib import Path
from typing import cast

from app.main_util import preserve_error_text
from app.service.problem.solution_metadata import normalize_expected_behavior

from .context import count_label
from .context_verification import _status_rule_expectation_display
from .run_lifecycle import (
    run_lifecycle_current_step,
    run_lifecycle_current_step_fields,
    run_lifecycle_status_label,
    verification_step_title,
)
from .run_view_lifecycle_card import (
    _verification_run_test_progress,
)


def _column_has_started_run(col: dict[str, object]) -> bool:
    status = cast(str, col.get("status") or "")
    if status and status not in {"queued", "pending"}:
        return True
    summary = cast(dict[str, object], col.get("summary") or {})
    tests = cast(list[dict[str, object]], summary.get("tests") or [])
    compile_diagnostics = cast(list[dict[str, object]], summary.get("compile_diagnostics") or [])
    if tests:
        return True
    if compile_diagnostics:
        return True
    summary_error = cast(str, summary.get("error") or "")
    if summary_error:
        return True
    return bool(col.get("tests_map"))


def _column_is_solve_main_run(col: dict[str, object]) -> bool:
    verification_source = _run_verification_source(col)
    if not verification_source:
        verification_source = cast(str, col.get("verification_source") or "")
    return verification_source == "verification.solve-main"


def _verification_runs(verification_details: dict[str, object]) -> list[dict[str, object]]:
    runs = cast(dict[str, dict[str, object]], verification_details.get("runs") or {})
    order = cast(list[str], verification_details.get("runs_order") or [])
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in [*order, *runs.keys()]:
        run_id = raw
        if (not run_id) or run_id in seen:
            continue
        row = runs.get(run_id)
        if row is None:
            continue
        seen.add(run_id)
        out.append(dict(row))
    return out


def _run_verification_source(run_row: dict[str, object]) -> str:
    summary = cast(dict[str, object], run_row.get("summary") or {})
    verification = cast(dict[str, object], summary.get("verification") or {})
    verification_source = cast(str | None, summary.get("verification_source"))
    if verification_source is not None:
        return verification_source
    verification_source = verification.get("source")
    if verification_source is not None:
        return cast(str, verification_source)
    verification_source = run_row.get("verification_source")
    if verification_source is not None:
        return cast(str, verification_source)
    return ""


def _run_status(run_row: dict[str, object]) -> str:
    return cast(str, run_row.get("status") or "")


def _compile_diagnostic_headline(summary: dict[str, object]) -> str:
    compile_diagnostics = cast(list[dict[str, object]], summary.get("compile_diagnostics") or [])
    if not compile_diagnostics:
        return ""
    first = compile_diagnostics[0]
    file_name = cast(str, first.get("file") or "")
    line = cast(int, first.get("line") or 0)
    column = cast(int, first.get("column") or 0)
    message = preserve_error_text(cast(str, first.get("message") or ""))
    if not message:
        return ""
    location = file_name
    if line > 0:
        location = f"{location}:{line}" if location else str(line)
    if column > 0:
        location = f"{location}:{column}" if location else str(column)
    if location:
        return f"{location}: {message}"
    return message


def _verification_stage_summary(
    verification_details: dict[str, object],
    stage_key: str,
) -> dict[str, object]:
    stage_results = cast(dict[str, dict[str, object]], verification_details.get("stage_results") or {})
    stage_summary = stage_results.get(stage_key)
    return {} if stage_summary is None else dict(stage_summary)


def _stage_lifecycle_status(stage_summary: dict[str, object]) -> str:
    status = cast(str, stage_summary.get("status") or "")
    if status == "ok":
        return "done"
    if status in {"failed", "cancelled"}:
        return "failed"
    if status == "running":
        return "running"
    if status in {"pending", "queued"}:
        return "pending"
    if status == "skipped":
        return "skipped"
    return ""


def _primary_stage_state(states: list[dict[str, str]]) -> dict[str, str]:
    priority = {"fail": 4, "running": 3, "pending": 2, "ok": 1}
    best: dict[str, str] = {}
    best_score = -1
    for item in states:
        tone = item.get("tone") or ""
        score = int(priority.get(tone, 0))
        if score > best_score:
            best = dict(item)
            best_score = score
    return best


def _hidden_input_stage_counts(
    *,
    row_test_stage_states: dict[str, list[dict[str, str]]],
    hidden_stage_runs: list[dict[str, object]],
    test_row_total: int,
) -> dict[str, int]:
    total = 0
    terminal = 0
    failed = 0
    running = 0
    pending = 0
    known = 0
    validated = 0
    generated = 0
    for states in row_test_stage_states.values():
        primary = _primary_stage_state(states)
        if not primary:
            continue
        known += 1
        tone = primary.get("tone") or ""
        if tone == "fail":
            failed += 1
            terminal += 1
        elif tone == "ok":
            terminal += 1
        elif tone == "running":
            running += 1
        else:
            pending += 1
        labels = set()
        for item in states:
            stage_key = item.get("stage_key")
            if stage_key:
                labels.add(stage_key)
        if "validated" in labels:
            validated += 1
        if "generated" in labels:
            generated += 1
    if hidden_stage_runs or known > 0:
        total = max(known, max(0, int(test_row_total)))
    else:
        total = known
    if total > known:
        pending += (total - known)
    hidden_running = 0
    hidden_failed = 0
    hidden_terminal = 0
    for row in hidden_stage_runs:
        status = _run_status(row)
        if status in {"queued", "pending", "running"}:
            hidden_running += 1
        elif status in {"failed", "cancelled"}:
            hidden_failed += 1
        elif status:
            hidden_terminal += 1
    return {
        "total": total,
        "terminal": terminal,
        "failed": failed,
        "running": running,
        "pending": pending,
        "validated": validated,
        "generated": generated,
        "hidden_run_count": len(hidden_stage_runs),
        "hidden_running": hidden_running,
        "hidden_failed": hidden_failed,
        "hidden_terminal": hidden_terminal,
    }


def _column_test_progress(col: dict[str, object] | None, *, fallback_tests: int) -> dict[str, int]:
    if col is None:
        return {"total": max(0, int(fallback_tests)), "completed": 0, "running": 0}
    tests_total = cast(int, col.get("tests_total") or 0)
    fallback_total = max(tests_total, int(fallback_tests))
    run_status = _run_status(col)
    progress = _verification_run_test_progress(
        built_columns=[col],
        run_statuses=[run_status],
        run_count=1,
        fallback_tests_per_solution=max(0, int(fallback_total)),
    )
    return {
        "total": max(0, cast(int, progress.get("total") or 0)),
        "completed": max(0, cast(int, progress.get("completed") or 0)),
        "running": max(0, cast(int, progress.get("running") or 0)),
    }


def _column_is_cancelled(col: dict[str, object]) -> bool:
    status = _run_status(col)
    if status == "cancelled":
        return True
    summary = cast(dict[str, object], col.get("summary") or {})
    if bool(summary.get("cancelled")):
        return True
    error_text = preserve_error_text(cast(str, col.get("error") or summary.get("error") or ""))
    return error_text == "verification cancelled by user"


def _run_is_cancelled(run_row: dict[str, object]) -> bool:
    if _run_status(run_row) == "cancelled":
        return True
    run_error = cast(str, run_row.get("error") or "")
    if preserve_error_text(run_error) == "verification cancelled by user":
        return True
    summary = cast(dict[str, object], run_row.get("summary") or {})
    if bool(summary.get("cancelled")):
        return True
    return preserve_error_text(cast(str, summary.get("error") or "")) == "verification cancelled by user"


def _first_terminal_run_error(columns: list[dict[str, object]]) -> str:
    for col in columns:
        status = _run_status(col)
        if status in {"queued", "pending", "running"}:
            continue
        error_text = preserve_error_text(cast(str, col.get("error") or ""))
        if not error_text:
            continue
        source_label = cast(str, col.get("title") or "")
        if source_label and not error_text.startswith(f"{source_label}:"):
            return f"{source_label}: {error_text}"
        return error_text
    return ""


def _build_verification_lifecycle_card(
    *,
    problem_slug: str,
    problem_id: int,
    workspace_id: int,
    actor_user_id: int,
    verification_id: str,
    verification_details: dict[str, object],
    columns: list[dict[str, object]],
    row_test_stage_states: dict[str, list[dict[str, str]]],
    test_row_total: int,
    detail_status: str,
    detail_running: bool,
    progress_reported: int,
    progress_total: int,
    matched_count: int,
    match_total: int,
) -> dict[str, object]:
    del problem_slug, problem_id, workspace_id, actor_user_id, verification_id, progress_reported

    persisted_status = cast(str, verification_details.get("status") or "")
    if persisted_status in {"ok", "failed", "cancelled"}:
        detail_status = persisted_status
    elif not detail_status:
        detail_status = persisted_status
    if not detail_status:
        detail_status = "running"
    safe_detail_running = detail_running and detail_status in {"running", "queued", "pending"}
    error_text = preserve_error_text(cast(str, verification_details.get("error") or ""))
    artifact_error_text = preserve_error_text(cast(str, verification_details.get("artifact_verification_error") or ""))
    generate_stage_summary = _verification_stage_summary(verification_details, "generate_input")
    generate_stage_status = _stage_lifecycle_status(generate_stage_summary)
    solve_main_stage_summary = _verification_stage_summary(verification_details, "solve_main")
    solve_main_stage_status = _stage_lifecycle_status(solve_main_stage_summary)

    raw_steps = cast(list[str], verification_details.get("steps") or [])
    step_ids: list[str] = []
    for token in raw_steps:
        if token in {"gen", "val", "run", "check"} and token not in step_ids:
            step_ids.append(token)
    if not step_ids:
        step_ids = ["gen", "val", "run", "check"]
    for token in ("gen", "val", "run", "check"):
        if token not in step_ids:
            step_ids.append(token)

    hidden_stage_runs = [
        row
        for row in _verification_runs(verification_details)
        if _run_verification_source(row) == "verification.generate-input"
    ]
    input_counts = _hidden_input_stage_counts(
        row_test_stage_states=row_test_stage_states,
        hidden_stage_runs=hidden_stage_runs,
        test_row_total=max(0, int(test_row_total)),
    )

    main_column = next((col for col in columns if _column_is_solve_main_run(col)), None)
    main_status = _run_status(main_column) if main_column is not None else ""
    fallback_tests = max(0, int(progress_total), int(test_row_total))
    main_progress = _column_test_progress(main_column, fallback_tests=fallback_tests)
    main_started = bool(main_column) and (
        _column_has_started_run(main_column) or main_status not in {"", "queued", "pending"}
    )
    main_cancelled = _column_is_cancelled(main_column) if main_column is not None else False
    main_error = "" if main_column is None else preserve_error_text(cast(str, main_column.get("error") or ""))

    visible_solution_columns = [col for col in columns if not _column_is_solve_main_run(col)]
    visible_statuses = [_run_status(col) for col in visible_solution_columns]
    visible_started = [col for col in visible_solution_columns if _column_has_started_run(col)]
    visible_progress = _verification_run_test_progress(
        built_columns=visible_solution_columns,
        run_statuses=visible_statuses,
        run_count=len(visible_solution_columns),
        fallback_tests_per_solution=fallback_tests,
    )
    visible_progress_total = cast(int, visible_progress.get("total") or 0)
    visible_progress_completed = cast(int, visible_progress.get("completed") or 0)
    visible_progress_running = cast(int, visible_progress.get("running") or 0)
    visible_run_count = len(visible_solution_columns)
    visible_completed_runs = sum(1 for token in visible_statuses if token and token not in {"queued", "pending", "running"})
    visible_running_runs = sum(1 for token in visible_statuses if token == "running")
    cancelled_visible_runs = sum(
        1
        for idx, token in enumerate(visible_statuses)
        if token in {"failed", "cancelled"}
        and not bool(visible_solution_columns[idx].get("execution_skipped"))
        and _column_is_cancelled(visible_solution_columns[idx])
    )
    visible_failed_runs = sum(
        1
        for idx, token in enumerate(visible_statuses)
        if token in {"failed", "cancelled"}
        and not bool(visible_solution_columns[idx].get("execution_skipped"))
        and not _column_is_cancelled(visible_solution_columns[idx])
    )
    visible_skipped_runs = sum(1 for col in visible_solution_columns if bool(col.get("execution_skipped")))
    all_visible_skipped = bool(visible_solution_columns) and visible_skipped_runs == len(visible_solution_columns)
    hidden_stage_cancelled = any(_run_is_cancelled(row) for row in hidden_stage_runs)

    status_by_step = {token: "pending" for token in step_ids}
    detail_by_step: dict[str, str] = {}
    step_facts: dict[str, list[dict[str, str]]] = {token: [] for token in step_ids}
    step_notes: dict[str, list[str]] = {token: [] for token in step_ids}

    def _step_add_fact(step_id: str, label: str, value: str, tone: str = "") -> None:
        token = step_id
        if token not in step_facts or (not label) or (not value):
            return
        step_facts[token].append(
            {
                "label": label,
                "value": value,
                "tone": tone,
            }
        )

    def _step_add_note(step_id: str, text: str) -> None:
        token = step_id
        if token not in step_notes or (not text):
            return
        if text not in step_notes[token]:
            step_notes[token].append(text)

    missing_lifecycle_evidence = (
        detail_status in {"failed", "cancelled"}
        and not generate_stage_status
        and not solve_main_stage_status
        and input_counts["total"] == 0
        and input_counts["hidden_failed"] == 0
        and input_counts["hidden_running"] == 0
        and main_column is None
        and not main_started
        and main_progress["total"] == 0
        and main_progress["completed"] == 0
        and main_progress["running"] == 0
        and visible_running_runs == 0
        and visible_completed_runs == 0
        and visible_progress_total == 0
        and visible_progress_completed == 0
        and visible_progress_running == 0
        and visible_failed_runs == 0
        and cancelled_visible_runs == 0
    )
    if missing_lifecycle_evidence:
        detail_by_step["gen"] = error_text or "verification failed before lifecycle state was recorded"
        status_by_step["gen"] = "failed"
        _step_add_note("gen", detail_by_step["gen"])
        status_by_step["val"] = "skipped"
        detail_by_step["val"] = "not executed (verification stopped before output generation)"
        _step_add_note("val", detail_by_step["val"])
        status_by_step["run"] = "skipped"
        detail_by_step["run"] = "not executed (verification stopped before solution runs started)"
        _step_add_note("run", detail_by_step["run"])
        status_by_step["check"] = "skipped"
        detail_by_step["check"] = "not executed (verification stopped before checks)"
        _step_add_note("check", detail_by_step["check"])
        _step_add_fact(
            "check",
            "Overall status",
            detail_status.upper(),
            tone="danger" if detail_status == "failed" else "",
        )

    # Step 1: Generate Inputs
    if missing_lifecycle_evidence:
        pass
    elif generate_stage_status:
        status_by_step["gen"] = generate_stage_status
    elif input_counts["failed"] > 0:
        status_by_step["gen"] = "failed"
    elif input_counts["total"] > 0:
        if input_counts["terminal"] >= input_counts["total"] and input_counts["running"] == 0 and input_counts["pending"] == 0:
            status_by_step["gen"] = "done"
        elif input_counts["hidden_failed"] > 0 and not hidden_stage_cancelled:
            status_by_step["gen"] = "failed"
        elif input_counts["running"] > 0 or input_counts["terminal"] > 0:
            status_by_step["gen"] = "running"
        else:
            status_by_step["gen"] = "pending"
    elif input_counts["hidden_failed"] > 0 and not hidden_stage_cancelled:
        status_by_step["gen"] = "failed"
    elif input_counts["hidden_running"] > 0:
        status_by_step["gen"] = "running"
    elif main_started or visible_started or detail_status in {"ok", "failed", "cancelled"}:
        status_by_step["gen"] = "done"
    elif detail_status in {"running", "queued", "pending"}:
        status_by_step["gen"] = "running"

    if missing_lifecycle_evidence:
        pass
    elif input_counts["total"] > 0:
        detail_by_step["gen"] = f"prepared tests {input_counts['terminal']}/{input_counts['total']}"
        _step_add_fact("gen", "Prepared tests", f"{input_counts['terminal']}/{input_counts['total']}")
        if input_counts["validated"] > 0:
            _step_add_fact("gen", "Validated tests", count_label(input_counts["validated"], "test"))
        if input_counts["generated"] > 0:
            _step_add_fact("gen", "Generated tests", count_label(input_counts["generated"], "test"))
        if input_counts["running"] > 0:
            _step_add_fact("gen", "Running tests", count_label(input_counts["running"], "test"))
    elif status_by_step["gen"] == "running":
        detail_by_step["gen"] = "generating inputs"
        _step_add_note("gen", "Waiting for input-stage testcase results.")
    if (not missing_lifecycle_evidence) and status_by_step["gen"] == "failed":
        hidden_error = preserve_error_text(cast(str, generate_stage_summary.get("error") or ""))
        if not hidden_error:
            hidden_error = _compile_diagnostic_headline(generate_stage_summary)
        if not hidden_error:
            for row in hidden_stage_runs:
                if _run_is_cancelled(row):
                    continue
                summary = cast(dict[str, object], row.get("summary") or {})
                hidden_error = preserve_error_text(cast(str, summary.get("error") or ""))
                if hidden_error:
                    break
                hidden_error = _compile_diagnostic_headline(summary)
                if hidden_error:
                    break
        detail_by_step["gen"] = (
            hidden_error
            or artifact_error_text
            or error_text
            or "verification failed before input generation state was recorded"
        )
        _step_add_note("gen", detail_by_step["gen"])

    # Step 2: Generate Outputs
    if missing_lifecycle_evidence:
        pass
    elif solve_main_stage_status:
        if status_by_step["gen"] == "failed" and solve_main_stage_status == "pending" and not main_started:
            status_by_step["val"] = "skipped"
        else:
            status_by_step["val"] = solve_main_stage_status
    elif status_by_step["gen"] == "failed" and not main_started:
        status_by_step["val"] = "skipped"
    elif main_column is None:
        if status_by_step["gen"] == "failed":
            status_by_step["val"] = "skipped"
        elif detail_status in {"failed", "cancelled"} and not visible_started:
            status_by_step["val"] = "failed"
        elif status_by_step["gen"] == "done" and detail_status in {"running", "queued", "pending"}:
            status_by_step["val"] = "pending"
        elif status_by_step["gen"] == "done" and detail_status in {"failed", "cancelled"}:
            status_by_step["val"] = "failed"
    else:
        if main_cancelled or main_status in {"failed", "cancelled"}:
            status_by_step["val"] = "failed"
        elif main_status in {"queued", "pending"} and not main_started:
            status_by_step["val"] = "pending"
        elif main_status == "ok" and (main_progress["total"] <= 0 or main_progress["completed"] >= main_progress["total"]):
            status_by_step["val"] = "done"
        elif main_started or main_progress["running"] > 0 or main_progress["completed"] > 0:
            status_by_step["val"] = "running"
        else:
            status_by_step["val"] = "pending"
    if missing_lifecycle_evidence:
        pass
    elif main_progress["total"] > 0:
        detail_by_step["val"] = f"generated outputs {main_progress['completed']}/{main_progress['total']}"
        _step_add_fact("val", "Generated outputs", f"{main_progress['completed']}/{main_progress['total']}")
        if main_progress["running"] > 0:
            _step_add_fact("val", "Running tests", count_label(main_progress["running"], "test"))
    elif status_by_step["val"] == "running":
        detail_by_step["val"] = "generating outputs"
    elif status_by_step["val"] == "pending":
        _step_add_note("val", "Waiting for accepted/main solution to start.")
    if (not missing_lifecycle_evidence) and status_by_step["val"] == "failed":
        if not detail_by_step.get("val"):
            detail_by_step["val"] = main_error or error_text or "verification failed before output generation state was recorded"
        _step_add_note("val", detail_by_step["val"])
    elif (not missing_lifecycle_evidence) and status_by_step["val"] == "skipped":
        detail_by_step["val"] = "not executed (input generation failed)"
        step_facts["val"] = []
        _step_add_note("val", detail_by_step["val"])

    # Step 3: Run Solutions
    blocked_solution_execution = (
        status_by_step["val"] in {"failed", "skipped"}
        and not visible_started
        and visible_completed_runs == 0
        and visible_running_runs == 0
        and visible_progress_completed == 0
        and visible_progress_running == 0
        and visible_failed_runs == 0
        and cancelled_visible_runs == 0
    )
    if missing_lifecycle_evidence:
        pass
    elif blocked_solution_execution:
        status_by_step["run"] = "skipped"
    elif not visible_solution_columns:
        status_by_step["run"] = "skipped"
    elif all_visible_skipped:
        status_by_step["run"] = "skipped"
    elif visible_running_runs > 0 or visible_progress_running > 0:
        status_by_step["run"] = "running"
    elif visible_failed_runs > 0 or cancelled_visible_runs > 0:
        status_by_step["run"] = "failed"
    elif visible_run_count > 0 and visible_completed_runs >= visible_run_count:
        status_by_step["run"] = "done"
    elif any(token in {"queued", "pending"} for token in visible_statuses):
        if status_by_step["val"] in {"failed", "skipped"} and detail_status in {"failed", "cancelled"}:
            status_by_step["run"] = "skipped"
        else:
            status_by_step["run"] = "pending"

    if missing_lifecycle_evidence:
        pass
    elif visible_run_count > 0:
        if all_visible_skipped:
            detail_by_step["run"] = "not executed (setup failed)"
            _step_add_fact("run", "Solutions skipped", f"{visible_run_count}/{visible_run_count}")
            _step_add_note("run", detail_by_step["run"])
        else:
            if status_by_step["run"] == "failed":
                detail_by_step["run"] = f"failed ({visible_completed_runs}/{visible_run_count} completed)"
            elif visible_progress_total > 0:
                detail_by_step["run"] = f"{visible_progress_completed}/{visible_progress_total} tests finished"
            else:
                detail_by_step["run"] = f"{visible_completed_runs}/{visible_run_count} solutions finished"
            _step_add_fact("run", "Solutions finished", f"{visible_completed_runs}/{visible_run_count}")
            if visible_progress_total > 0:
                _step_add_fact("run", "Tests finished", f"{visible_progress_completed}/{visible_progress_total}")
            if visible_running_runs > 0:
                _step_add_fact("run", "Running solutions", count_label(visible_running_runs, "solution"))
            if visible_progress_running > 0:
                _step_add_fact("run", "Running tests", count_label(visible_progress_running, "test"))
            if cancelled_visible_runs > 0:
                _step_add_fact("run", "Cancelled solutions", count_label(cancelled_visible_runs, "solution"), tone="danger")
            if visible_failed_runs - cancelled_visible_runs > 0:
                _step_add_fact("run", "Failed solutions", count_label(visible_failed_runs - cancelled_visible_runs, "solution"), tone="danger")
            if visible_skipped_runs > 0:
                _step_add_fact("run", "Skipped solutions", count_label(visible_skipped_runs, "solution"))
            if status_by_step["run"] == "failed":
                _step_add_note("run", _first_terminal_run_error(visible_solution_columns) or error_text or "solution execution failed")
            elif status_by_step["run"] == "pending":
                _step_add_note("run", "Waiting for non-main solutions to start.")
    if (not missing_lifecycle_evidence) and status_by_step["run"] == "skipped" and blocked_solution_execution:
        detail_by_step["run"] = (
            "not executed (input generation failed)"
            if status_by_step["val"] == "skipped"
            else "not executed (output generation failed)"
        )
        step_facts["run"] = []
        _step_add_note("run", detail_by_step["run"])

    # Step 4: Check Expectations
    visible_columns = list(columns)
    visible_total = int(match_total)
    visible_started_any = any(
        _column_has_started_run(col) or _run_status(col) not in {"", "queued", "pending"}
        for col in visible_columns
    )
    visible_terminal = bool(visible_columns) and all(
        (
            _run_status(col) not in {"queued", "pending", "running"}
            or _column_is_cancelled(col)
        )
        for col in visible_columns
    )
    blocked_expectation_check = (
        detail_status in {"failed", "cancelled"}
        and not visible_started_any
        and any(status_by_step[token] in {"failed", "skipped"} for token in ("gen", "val", "run"))
    )
    if missing_lifecycle_evidence:
        pass
    elif blocked_expectation_check:
        status_by_step["check"] = "skipped"
    elif visible_total <= 0:
        status_by_step["check"] = "pending"
    elif visible_terminal:
        status_by_step["check"] = "done" if int(matched_count) >= visible_total and detail_status == "ok" else "failed"
    elif detail_status in {"failed", "cancelled"} and any(
        status_by_step[token] == "failed" for token in ("gen", "val", "run")
    ):
        status_by_step["check"] = "failed"
    elif safe_detail_running or any(_run_status(col) in {"queued", "pending", "running"} for col in visible_columns):
        status_by_step["check"] = "pending"
    elif detail_status in {"failed", "cancelled"}:
        status_by_step["check"] = "failed"

    if missing_lifecycle_evidence:
        pass
    elif visible_total > 0:
        detail_by_step["check"] = f"matched expectations {int(matched_count)}/{visible_total}"
        _step_add_fact("check", "Matched expectations", f"{int(matched_count)}/{visible_total}")
    if (not missing_lifecycle_evidence) and status_by_step["check"] == "skipped":
        detail_by_step["check"] = "not executed (verification stopped before checks)"
        step_facts["check"] = []
        _step_add_note("check", detail_by_step["check"])
    if detail_status and (not missing_lifecycle_evidence):
        _step_add_fact(
            "check",
            "Overall status",
            detail_status.upper(),
            tone="ok" if detail_status == "ok" else "danger" if detail_status == "failed" else "",
        )

    solutions_obj = cast(list[dict[str, object]], verification_details.get("solutions") or [])
    if solutions_obj:
        mismatch_lines: list[str] = []
        solution_total = 0
        solution_matched = 0
        for item in solutions_obj:
            solution_total += 1
            if bool(item.get("matched")):
                solution_matched += 1
                continue
            source_path = cast(str, item.get("source_path") or "")
            source_label = Path(source_path).name if source_path else f"solution {solution_total}"
            expected_behavior = normalize_expected_behavior(item.get("expected_behavior"))
            reason_text = preserve_error_text(cast(str, item.get("reason") or ""))
            run_error_text = preserve_error_text(cast(str, item.get("error") or ""))
            if reason_text and run_error_text:
                detail_text = f"{reason_text}: {run_error_text}"
            elif reason_text:
                detail_text = reason_text
            elif run_error_text:
                detail_text = run_error_text
            else:
                detail_text = _status_rule_expectation_display(expected_behavior)
            mismatch_lines.append(f"{source_label}: {detail_text}")
        if solution_total > 0:
            _step_add_fact("check", "Solutions matched", f"{solution_matched}/{solution_total}")
        for line in mismatch_lines[:4]:
            _step_add_note("check", line)
        if len(mismatch_lines) > 4:
            _step_add_note("check", f"+{len(mismatch_lines) - 4} more mismatches")
    if error_text and error_text not in step_notes["check"]:
        _step_add_note("check", error_text)

    steps: list[dict[str, object]] = []
    for idx, token in enumerate(step_ids, start=1):
        step_status = status_by_step.get(token) or "pending"
        step_detail = detail_by_step.get(token) or ""
        step_fact_items = step_facts.get(token) or []
        step_note_items = step_notes.get(token) or []
        steps.append(
            {
                "index": idx,
                "id": token,
                "title": verification_step_title(token),
                "status": step_status,
                "status_label": run_lifecycle_status_label(step_status),
                "detail": step_detail,
                "facts": step_fact_items,
                "notes": step_note_items,
            }
        )
    current_step_index, current_step_title = run_lifecycle_current_step(steps)
    current_step_status, current_step_status_label, current_step_detail = run_lifecycle_current_step_fields(steps, current_step_index)
    return {
        "id": "verification",
        "title": "Verification Progress",
        "total_steps": len(steps),
        "current_step_index": current_step_index,
        "current_step_title": current_step_title,
        "current_step_status": current_step_status,
        "current_step_status_label": current_step_status_label,
        "current_step_detail": current_step_detail,
        "summary": "",
        "steps": steps,
    }
