from pathlib import Path
from collections.abc import Sequence
from typing import TypedDict
from fastapi import HTTPException
from app.impl.runtime.dependency import runtime
from app.service.repository.workspace import WorkspaceContext
from app.impl.workspace.artifact import verification_artifact_file, verification_blob_virtual_rel
from app.impl.workspace.context import count_label
from app.impl.workspace.context_operation import workspace_rel_file_exists
from app.impl.workspace.context_run_detail import (
    _decorate_compile_diagnostics,
    _normalize_diagnostics,
    _run_detail_preview_from_bytes,
    normalize_run_id_token,
    normalize_run_test_name_token,
    _run_detail_preview_unavailable,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
)
from app.service.judgehost.callback.runpipe_transcript import parse_runpipe_transcript
from app.service.platform.error_text import bounded_display_text
from app.service.platform.workspace_path import (
    normalize_optional_component_source_path_safe,
    normalize_workspace_rel_path,
)
from app.service.verification.types import VerificationDetail, VerificationTaskRow
from app.service.verification.detail_read_model import (
    VerificationProgramDetailRow,
    verification_case_test_row,
)
from app.service.verification.lifecycle import VerificationSnapshotRecord
from app.service.execution.codec import compile_diagnostics_payload
from app.service.verification.types import VerificationTaskStatus
from app.service.platform.process import is_canonical_artifact_id
from app.impl.workspace.run_view_lifecycle_card import _verification_tests_meta_stats
from app.impl.workspace.run_test_generation import (
    build_test_generation_views,
    generation_warning_message,
)
from app.impl.workspace.run_view_model import (
    DiagnosticEntry, RunCaseCell, RunCellDetail, RunCellView, RunColumn,
    RunColumnBase, RunDetailContext, RunDetailPreview, RunPassView, RunSanityTask,
    RunSanityView, RunTestDetailContext, RunTestNameCell, RunTestRow,
    RunTranscriptView, RunVerificationLogs, TestGenerationView,
)
from app.service.verification.types import (
    VerificationCaseTestRow, VerificationSanityCheckRow,
    VerificationSanityMessageRow, VerificationTestMetadata,
)
import app.service.verification.read_model
from app.service.verification.result_match import (
    analyze_program_result,
    expected_status_rule,
    run_verdict_short,
    status_rule_expected_display,
)
from app.service.verification.failure_display import verification_solution_failure_hint
from app.impl.workspace.run_view_list import (
    _latest_iso_timestamp,
    _run_cell_kind,
    _run_expected_behavior_from_summary,
    _run_task_kind_from_summary,
    _is_main_correct_task_kind,
    _run_test_answer_name,
    _run_test_sort_key,
)
from app.impl.workspace.run_display import (
    rewrite_failure_reason_with_source,
    run_cpu_wall_ms_text,
    run_error_display,
    run_memory_mb_text,
)
from app.service.verification.runtime_threshold import (
    SUMMARY_RUNTIME_THRESHOLD_CHECK,
    evaluate_summary_runtime_threshold,
    time_limit_ms_from_run_config_json,
)

_TASK_KIND_MAIN_CORRECT = "main-correct"
_TASK_KIND_SOLUTION_RUN = "solution-run"
_SANITY_STATUS_TOKENS = {"ok", "passed", "pending", "running", "warning", "failed", "skipped"}
_SANITY_CHECK_LABELS = {
    "empty_output_stability": "Empty output stability",
    "unicode_output_stability": "Unicode output stability",
    "custom_sample_output": "Custom sample output",
    SUMMARY_RUNTIME_THRESHOLD_CHECK: "Summary runtime threshold",
    "boundary_coverage": "Boundary coverage",
}


class SanityPayload(TypedDict):
    sanity_status: str
    sanity_checked_count: int
    sanity_checks: list[str]
    sanity_check_results: list[VerificationSanityCheckRow]
    validation_status: str
    validated_count: int
    failed_step: str
    failed_check: str
    failed_test: str
    error: str


def _canonical_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"verification detail {field} must be an integer")
    return value


def _detail_int(value: object, *, field: str, default: int = 0) -> int:
    if value is None:
        return default
    return _canonical_int(value, field=field)


def _detail_text(value: object, *, field: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise RuntimeError(f"verification detail {field} must be text")
    return value


def _detail_string_list(value: object, *, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuntimeError(f"verification detail {field} must be a list")
    values: list[str] = []
    for index, item in enumerate(value):
        values.append(_detail_text(item, field=f"{field}[{index}]"))
    return values


def _run_result_kind(
    expected_behavior: str,
    *,
    matched: bool,
    completed: bool,
    observed_pass: bool,
    got_short: str,
) -> str:
    if got_short == "FL":
        return "fail"
    if expected_behavior == "unknown" or not completed:
        return "neutral"
    if not matched:
        return "fail"
    if expected_behavior == "accepted":
        return "ok"
    return "neutral" if observed_pass else "expected-nonac"


def _sanity_checks_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item or "") for item in raw if str(item or "")]


def _sanity_check_label(check_name: str) -> str:
    token = str(check_name or "")
    return _SANITY_CHECK_LABELS.get(token, token.replace("_", " ") or "Sanity check")


def _build_sanity_payload(verification_details: VerificationDetail) -> SanityPayload:
    sanity_status = str(verification_details.get("sanity_status") or "").strip().lower()
    if sanity_status not in _SANITY_STATUS_TOKENS:
        sanity_status = "unknown"
    validation_status = str(verification_details.get("validation_status") or "").strip().lower()
    if validation_status not in _SANITY_STATUS_TOKENS:
        validation_status = "unknown"
    return {
        "sanity_status": sanity_status,
        "sanity_checked_count": _canonical_int(
            verification_details.get("sanity_checked_count", 0),
            field="sanity_checked_count",
        ),
        "sanity_checks": _sanity_checks_list(verification_details.get("sanity_checks")),
        "sanity_check_results": verification_details.get("sanity_check_results", []),
        "validation_status": validation_status,
        "validated_count": _canonical_int(
            verification_details.get("validated_count", 0),
            field="validated_count",
        ),
        "failed_step": str(verification_details.get("failed_step") or ""),
        "failed_check": str(verification_details.get("failed_check") or ""),
        "failed_test": str(verification_details.get("failed_test") or ""),
        "error": str(verification_details.get("error") or ""),
    }


def _sanity_status_tone(status: str) -> str:
    if status == "passed":
        return "ok"
    if status in {"warning", "failed"}:
        return "warn"
    if status in {"pending", "running"}:
        return "info"
    return "muted"


def _sanity_reason(payload: SanityPayload) -> str:
    error = bounded_display_text(
        str(payload.get("error") or ""),
        limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
    )
    if error:
        return error
    status = str(payload.get("sanity_status") or "")
    failed_check = str(payload.get("failed_check") or "")
    if status == "warning" and failed_check:
        return f"{_sanity_check_label(failed_check)} has warning"
    if status == "failed" and failed_check:
        return f"{_sanity_check_label(failed_check)} failed"
    return ""


def _sanity_messages(raw_messages: list[VerificationSanityMessageRow]) -> list[VerificationSanityMessageRow]:
    messages: list[VerificationSanityMessageRow] = []
    for raw in raw_messages:
        message = bounded_display_text(
            str(raw.get("message") or ""),
            limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
        )
        if not message:
            continue
        messages.append(
            {
                "severity": str(raw.get("severity") or ""),
                "test_name": str(raw.get("test_name") or ""),
                "message": message,
            }
        )
    return messages


def _sanity_task_rows_from_results(payload: SanityPayload) -> list[RunSanityTask]:
    status = str(payload.get("sanity_status") or "")
    results = payload["sanity_check_results"]
    if not results:
        failed_check = str(payload.get("failed_check") or "")
        if failed_check and status in {"warning", "failed"}:
            message = bounded_display_text(
                str(payload.get("error") or _sanity_reason(payload)),
                limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
            )
            messages: list[VerificationSanityMessageRow] = (
                [
                    {
                        "severity": status,
                        "test_name": str(payload.get("failed_test") or ""),
                        "message": message,
                    }
                ]
                if message
                else []
            )
            return [
                {
                    "name": failed_check,
                    "label": _sanity_check_label(failed_check),
                    "status": status,
                    "tone": _sanity_status_tone(status),
                    "detail": ""
                    if messages
                    else bounded_display_text(
                        _sanity_reason(payload),
                        limit_bytes=runtime().config_values.integer(
                            "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                        ),
                    ),
                    "messages": messages,
                }
            ]
        return []
    rows: list[RunSanityTask] = []
    for item in results:
        check_name = item["name"]
        if not check_name:
            continue
        row_status = str(item.get("status") or "")
        if not row_status:
            row_status = status if status in {"pending", "running", "skipped"} else "passed"
        row_messages = _sanity_messages(
            item["messages"]
        )
        detail = ""
        if not row_messages:
            if row_status == "passed":
                detail = "completed"
            elif row_status == "pending":
                detail = "waiting for sanity checks"
            elif row_status == "running":
                detail = "running"
            elif row_status == "skipped":
                detail = "not run"
        rows.append(
            {
                "name": check_name,
                "label": _sanity_check_label(check_name),
                "status": row_status,
                "tone": _sanity_status_tone(row_status),
                "detail": bounded_display_text(
                    detail,
                    limit_bytes=runtime().config_values.integer(
                        "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                    ),
                ),
                "messages": row_messages,
            }
        )
    return rows


def _detail_sanity_context(
    verification_id: str,
    verification_details: VerificationDetail,
) -> RunSanityView:
    if not verification_id:
        return {
            "available": False,
            "status": "unknown",
            "reason": "",
            "tasks": [],
            "attention_tasks": [],
            "task_count": 0,
            "ran_count": 0,
            "checked_count": 0,
        }
    payload = _build_sanity_payload(verification_details)
    tasks = _sanity_task_rows_from_results(payload)
    attention_tasks = [
        task
        for task in tasks
        if str(task.get("status") or "") in {"warning", "failed"} or bool(task.get("messages"))
    ]
    return {
        "available": True,
        "status": str(payload["sanity_status"]),
        "reason": _sanity_reason(payload),
        "tasks": tasks,
        "attention_tasks": attention_tasks,
        "task_count": len(tasks),
        "ran_count": sum(
            1 for task in tasks if str(task.get("status") or "") in {"passed", "warning", "failed"}
        ),
        "checked_count": _canonical_int(
            payload["sanity_checked_count"],
            field="sanity_checked_count",
        ),
    }


def _missing_solution_cell(task_status: str) -> RunCellView:
    if task_status == VerificationTaskStatus.LEASED:
        return {
            "text": "..",
            "short": "..",
            "metrics": "running",
            "kind": "running",
            "text_tone": "",
            "detail": None,
        }
    if task_status == VerificationTaskStatus.FAILED:
        return {
            "text": "FL",
            "short": "FL",
            "metrics": "failed",
            "kind": "fail",
            "text_tone": "",
            "detail": None,
        }
    if task_status == VerificationTaskStatus.CANCELLED:
        return {
            "text": "--",
            "short": "--",
            "metrics": "cancelled",
            "kind": "neutral",
            "text_tone": "",
            "detail": None,
        }
    return {
        "text": "..",
        "short": "..",
        "metrics": "",
        "kind": "neutral",
        "text_tone": "",
        "detail": None,
    }


def _test_name_cell(
    *,
    actual_test_name: str,
    fallback_name: str,
    is_placeholder: bool,
    note: dict[str, str],
    has_detail: bool,
) -> RunTestNameCell:
    tone = str(note.get("tone") or "")
    note_text = str(note.get("text") or "")
    note_detail = str(note.get("detail") or "")
    if is_placeholder and (not actual_test_name):
        return {
            "kind": "neutral",
            "text": "",
            "short": "..",
            "meta": "generating",
            "detail": note_detail,
            "clickable": False,
        }
    if tone in {"running", "pending"}:
        visible_name = actual_test_name or fallback_name
        meta = note_text.removeprefix(".. ").strip()
        if tone == "pending":
            meta = ""
        if visible_name:
            return {
                "kind": "running" if tone == "running" else "neutral",
                "text": visible_name,
                "short": "",
                "meta": meta or ("running" if tone == "running" else ""),
                "detail": note_detail,
                "clickable": False,
            }
        short = note_text
        if note_text.startswith(".. "):
            short = ".."
        if tone == "pending":
            short = ".."
        return {
            "kind": "running" if tone == "running" else "neutral",
            "text": "",
            "short": short or "..",
            "meta": meta or ("running" if tone == "running" else ""),
            "detail": note_detail,
            "clickable": False,
        }
    visible_name = actual_test_name or fallback_name
    kind = "neutral"
    if tone == "ok":
        kind = "ok"
    elif tone == "fail":
        kind = "fail"
    elif tone == "warn":
        kind = "warn"
    elif is_placeholder:
        kind = "neutral"
    return {
        "kind": kind,
        "text": visible_name,
        "short": "",
        "meta": "",
        "detail": note_detail,
        "clickable": bool(actual_test_name and has_detail and (not is_placeholder)),
    }


def _case_cell(
    item: VerificationCaseTestRow, *, idx: int, test_name: str, expected_behavior: str,
    verification_details: VerificationDetail, include_row_details: bool,
    detail_compile_error: str, detail_compile_diagnostics: list[DiagnosticEntry],
    display_limit: int, time_tone: str,
) -> RunCaseCell:
    verdict = _detail_text(
        item.get("verdict"),
        field=f"program.tests[{idx - 1}].verdict",
    ).upper() or "-"
    verdict_short = run_verdict_short(verdict)
    time_ms = _detail_int(
        item.get("time_ms"),
        field=f"program.tests[{idx - 1}].time_ms",
    )
    time_user_ms = _detail_int(
        item.get("time_user_ms"),
        field=f"program.tests[{idx - 1}].time_user_ms",
        default=time_ms,
    )
    time_wall_ms = _detail_int(
        item.get("time_wall_ms"),
        field=f"program.tests[{idx - 1}].time_wall_ms",
        default=time_user_ms,
    )
    memory_kb = _detail_int(
        item.get("memory_kb"),
        field=f"program.tests[{idx - 1}].memory_kb",
    )
    memory_mb_text = run_memory_mb_text(memory_kb)
    detail_payload: RunCellDetail | None = None
    if include_row_details:
        passes = item["passes"]
        late_diagnostic_text = bounded_display_text(
            _detail_text(
                item.get("late_diagnostic_text"),
                field=f"program.tests[{idx - 1}].late_diagnostic_text",
            ),
            limit_bytes=display_limit,
        )
        feedback_display = "-"
        inline_feedback = bounded_display_text(
            _detail_text(
                item["message"],
                field=f"program.tests[{idx - 1}].feedback",
            ),
            limit_bytes=runtime().config_values.integer(
                "AUX_DISPLAY_TEXT_LIMIT_BYTES"
            ),
        )
        feedback_total = len(item["feedback_files"])
        feedback_items = item["feedback_files"][:max(1, runtime().config_values.integer("RUN_TEST_FEEDBACK_FILE_LIST_LIMIT"))]
        test_stem = Path(str(test_name)).stem
        checker_log_rel = f"feedback_dir/{test_stem}/checker.log" if test_stem else ""
        feedback_rel = feedback_items[0] if feedback_items else ""
        if inline_feedback:
            feedback_display = inline_feedback
        feedback_truncated = feedback_total > len(feedback_items)
        if feedback_truncated:
            hidden_count = max(0, feedback_total - len(feedback_items))
            if hidden_count > 0 and feedback_display != "-":
                feedback_display = (
                    f"{feedback_display} (+{hidden_count} more)"
                    if feedback_display != "-"
                    else f'+{count_label(hidden_count, "file")}'
                )
        pass_rows: list[RunPassView] = []
        if passes:
            for pass_index, pass_item in enumerate(passes):
                pass_field = f"program.tests[{idx - 1}].passes[{pass_index}]"
                pass_verdict = _detail_text(
                    pass_item.get("verdict"),
                    field=f"{pass_field}.verdict",
                ).upper() or "-"
                pass_verdict_short = run_verdict_short(pass_verdict)
                pass_time_user_ms = _detail_int(
                    pass_item.get("time_user_ms")
                    if pass_item.get("time_user_ms") is not None
                    else pass_item.get("time_ms"),
                    field=f"{pass_field}.time_user_ms",
                )
                pass_time_wall_ms = _detail_int(
                    pass_item.get("time_wall_ms"),
                    field=f"{pass_field}.time_wall_ms",
                    default=pass_time_user_ms,
                )
                pass_memory_kb = _detail_int(
                    pass_item.get("memory_kb"),
                    field=f"{pass_field}.memory_kb",
                )
                pass_feedback = bounded_display_text(
                    _detail_text(
                        pass_item.get("feedback"),
                        field=f"{pass_field}.feedback",
                    ),
                    limit_bytes=runtime().config_values.integer(
                        "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                    ),
                )
                row_feedback_display = pass_feedback or feedback_display
                output_rel = _detail_text(
                    pass_item.get("output_ref"),
                    field=f"{pass_field}.output_ref",
                )
                pass_number = _detail_int(
                    pass_item.get("pass"),
                    field=f"{pass_field}.pass",
                )
                pass_time_display = run_cpu_wall_ms_text(
                    pass_time_user_ms, pass_time_wall_ms
                )
                pass_memory_display = run_memory_mb_text(pass_memory_kb)
                pass_rows.append(
                    {
                        "pass_number": pass_number,
                        "pass_label": f"Pass {pass_number}",
                        "capture_status": str(pass_item.get("capture_status") or ""),
                        "verdict_short": pass_verdict_short,
                        "text_tone": "",
                        "kind": _run_cell_kind(pass_verdict, expected_behavior),
                        "time_display": pass_time_display,
                        "time_tone": time_tone,
                        "memory_display": pass_memory_display,
                        "status_display": f"{pass_verdict_short} \u00b7 {pass_time_display} \u00b7 {pass_memory_display}",
                        "feedback_display": row_feedback_display,
                        "output_task_id": "",
                        "input_ref": str(pass_item.get("input_ref") or ""),
                        "output_rel": str(output_rel),
                        "transcript_rel": str(pass_item.get("transcript_ref") or ""),
                        "judge_message_rel": str(pass_item.get("judge_message_ref") or ""),
                        "checker_log_rel": checker_log_rel,
                        "feedback_rel": feedback_rel,
                    }
                )
        if not pass_rows:
            output_rel = _detail_text(
                item.get("output_ref"),
                field=f"program.tests[{idx - 1}].output_ref",
            )
            output_task_id = ""
            time_display = run_cpu_wall_ms_text(time_user_ms, time_wall_ms)
            pass_rows.append(
                {
                    "pass_number": 1,
                    "pass_label": "Pass 1",
                    "capture_status": "",
                    "verdict_short": verdict_short,
                    "text_tone": "",
                    "kind": _run_cell_kind(verdict, expected_behavior),
                    "time_display": time_display,
                    "time_tone": time_tone,
                    "memory_display": memory_mb_text,
                    "status_display": f"{verdict_short} \u00b7 {time_display} \u00b7 {memory_mb_text}",
                    "feedback_display": feedback_display,
                    "input_ref": "",
                    "output_rel": str(output_rel),
                    "output_task_id": output_task_id,
                    "transcript_rel": "",
                    "judge_message_rel": "",
                    "checker_log_rel": checker_log_rel,
                    "feedback_rel": feedback_rel,
                }
            )
        final_index = len(pass_rows) - 1
        for candidate_index in range(len(pass_rows) - 1, -1, -1):
            candidate = pass_rows[candidate_index]
            verdict_token = candidate.get("verdict_short") or ""
            if verdict_token and verdict_token not in {"--", "-"}:
                final_index = candidate_index
                break
        if late_diagnostic_text and pass_rows:
            final_feedback = str(pass_rows[final_index].get("feedback_display") or "")
            if late_diagnostic_text not in final_feedback:
                pass_rows[final_index]["feedback_display"] = bounded_display_text(
                    "\n\n".join(
                        value
                        for value in (
                            "" if final_feedback == "-" else final_feedback,
                            late_diagnostic_text,
                        )
                        if value
                    ),
                    limit_bytes=display_limit,
                )
        final_row = pass_rows[final_index].copy()
        detail_payload = {
            "verdict": verdict,
            "verdict_short": verdict_short,
            "time_display": f"{time_ms}ms",
            "time_tone": time_tone,
            "memory_display": memory_mb_text,
            "status_display": f"{verdict_short} \u00b7 {run_cpu_wall_ms_text(time_user_ms, time_wall_ms)} \u00b7 {memory_mb_text}",
            "feedback_display": feedback_display,
            "pass_rows": pass_rows,
            "final_row": final_row,
            "is_multi_pass": bool(
                _detail_int(
                    verification_details.get("pass_limit"),
                    field="pass_limit",
                    default=1,
                )
                > 1
                or len(pass_rows) > 1
            ),
            "compile_error_display": detail_compile_error,
            "compile_diagnostics": detail_compile_diagnostics,
            "late_diagnostics": list(item["late_diagnostics"]),
        }
    return {
        "verdict": verdict,
        "time_ms": time_ms,
        "memory_kb": memory_kb,
        "text": verdict_short,
        "short": verdict_short,
        "metrics": f"{time_ms}ms/{memory_mb_text}",
        "time_display": f"{time_ms}ms",
        "time_tone": time_tone,
        "memory_display": memory_mb_text,
        "kind": _run_cell_kind(verdict, expected_behavior),
        "text_tone": "",
        "detail": detail_payload,
        "detail_available": True,
    }


def _test_detail_rows(
    *, target_tests: list[str], row_index_by_test: dict[str, int], columns: Sequence[RunColumnBase],
    test_generation_views: dict[str, TestGenerationView], row_generate_notes: dict[str, dict[str, str]],
    source_verification_id: str, problem_slug: str, username: str,
    detail_is_main_correct_run: bool = False,
) -> list[RunTestRow]:
    detail_rows: list[RunTestRow] = []
    def _verification_artifact_preview(
        verification_id: str, rel_path: str
    ) -> RunDetailPreview:
        safe_verification_id = verification_id or ""
        safe_rel_path = (rel_path or "").lstrip("/")
        if (
            not problem_slug
            or not username
            or (not safe_rel_path)
            or (not is_canonical_artifact_id(safe_verification_id))
        ):
            return _run_detail_preview_unavailable("missing")
        resolved = verification_artifact_file(safe_verification_id, safe_rel_path)
        if resolved is None:
            return _run_detail_preview_unavailable("missing")
        payload_file, _filename = resolved
        with payload_file.path.open("rb") as stream:
            blob = stream.read(
                runtime().config_values.integer("RUN_DETAIL_PREVIEW_MAX_BYTES") + 1
            )
        return _run_detail_preview_from_bytes(
            blob,
            verification_id=safe_verification_id,
            rel_path=safe_rel_path,
        )

    def _verification_output_preview(
        verification_id: str, task_id: str, test_name: str
    ) -> RunDetailPreview:
        safe_verification_id = verification_id or ""
        safe_task_id = str(task_id or "").strip()
        test_stem = Path(test_name).stem
        filename = f"{test_stem}.out" if test_stem else "program.out"
        if (
            not problem_slug
            or not username
            or (not safe_task_id)
            or (not filename)
            or (not is_canonical_artifact_id(safe_verification_id))
        ):
            return _run_detail_preview_unavailable("missing")
        virtual_rel = f"output/{safe_task_id}/{filename}"
        return _verification_artifact_preview(safe_verification_id, virtual_rel)

    def _verification_blob_preview(
        verification_id: str,
        rel_path: str,
    ) -> RunDetailPreview:
        safe_verification_id = verification_id or ""
        safe_rel_path = (rel_path or "").lstrip("/")
        if (
            not problem_slug
            or not username
            or (not safe_rel_path)
            or (not is_canonical_artifact_id(safe_verification_id))
        ):
            return _run_detail_preview_unavailable("missing")
        if not safe_rel_path.startswith("blob://"):
            return _run_detail_preview_unavailable("missing")
        virtual_rel = verification_blob_virtual_rel(
            safe_rel_path, filename=Path(safe_rel_path).name
        )
        if not virtual_rel:
            return _run_detail_preview_unavailable("missing")
        return _verification_artifact_preview(safe_verification_id, virtual_rel)

    def _verification_transcript(
        verification_id: str,
        rel_path: str,
        *,
        unavailable_message: str,
    ) -> RunTranscriptView:
        safe_verification_id = verification_id or ""
        safe_rel_path = (rel_path or "").lstrip("/")
        unavailable: RunTranscriptView = {
            "available": False,
            "state": "unavailable",
            "events": [],
            "events_shown": 0,
            "events_total": 0,
            "events_omitted": 0,
            "raw_size_bytes": 0,
            "error_offset": None,
            "error_reason": None,
            "download_verification_id": "",
            "download_rel_path": "",
            "message": unavailable_message,
        }
        if (
            not problem_slug
            or not username
            or (not safe_rel_path)
            or (not is_canonical_artifact_id(safe_verification_id))
        ):
            return unavailable
        if not safe_rel_path.startswith("blob://"):
            return unavailable
        virtual_rel = verification_blob_virtual_rel(
            safe_rel_path, filename=Path(safe_rel_path).name
        )
        if not virtual_rel:
            return unavailable
        resolved = verification_artifact_file(safe_verification_id, virtual_rel)
        if resolved is None:
            return unavailable
        payload_file, _filename = resolved
        with payload_file.path.open("rb") as stream:
            parsed = parse_runpipe_transcript(
                stream,
                raw_size_bytes=payload_file.size,
            )
        return {
            "available": True,
            **parsed,
            "download_verification_id": safe_verification_id,
            "download_rel_path": virtual_rel,
            "message": "",
        }

    for test_name in target_tests:
        row_index = int(row_index_by_test.get(test_name) or 0)
        if row_index <= 0:
            continue
        generation_view = test_generation_views.get(test_name)
        generation_terminal = bool(generation_view is not None and generation_view["terminal"])
        generation_alert = (
            generation_view
            if generation_view is not None and generation_view["alert_message"]
            else None
        )
        input_rel = f"tests/{test_name}"
        answer_name = _run_test_answer_name(test_name)
        answer_rel = f"ans/{answer_name}" if answer_name else ""
        row_is_interactive = any(
            (col.get("mode") or "") == "interactive"
            and col["tests_map"].get(test_name) is not None
            for col in columns
        )
        input_preview = _run_detail_preview_unavailable("not applicable")
        answer_preview = _run_detail_preview_unavailable("not applicable")
        if not row_is_interactive:
            input_preview = _verification_artifact_preview(source_verification_id, input_rel)
            answer_preview = (
                _verification_artifact_preview(source_verification_id, answer_rel)
                if answer_rel
                else _run_detail_preview_unavailable("missing")
            )
        detail_cells: list[RunCellView] = []
        for col in columns:
            cell = col["tests_map"].get(test_name)
            if cell is None:
                detail_cells.append(
                    {
                        "text": "--",
                        "short": "--",
                        "metrics": "-",
                        "kind": "neutral",
                        "text_tone": "",
                        "detail": None,
                    }
                )
                continue
            detail_raw = cell.get("detail")
            detail_payload = (
                detail_raw.copy()
                if detail_raw is not None
                else None
            )
            if detail_payload is not None:
                interactive_mode = (col.get("mode") or "") == "interactive"
                pass_rows_payload: list[RunPassView] = []
                pass_rows_raw = detail_payload["pass_rows"]
                for pass_item in pass_rows_raw:
                    row_payload = pass_item.copy()
                    output_rel = _detail_text(
                        row_payload.get("output_rel"),
                        field="cell.detail.pass.output_rel",
                    )
                    output_task_id = _detail_text(
                        row_payload.get("output_task_id"),
                        field="cell.detail.pass.output_task_id",
                    )
                    output_preview = _run_detail_preview_unavailable("missing")
                    if output_rel and not interactive_mode:
                        if output_task_id and source_verification_id:
                            output_preview = _verification_output_preview(
                                source_verification_id, output_task_id, test_name
                            )
                        else:
                            output_preview = _verification_blob_preview(
                                source_verification_id, output_rel
                            )
                    row_payload["output_preview"] = output_preview
                    capture_status = str(row_payload.get("capture_status") or "")
                    capture_complete = capture_status == "complete"
                    input_ref = str(row_payload.get("input_ref") or "")
                    pass_input_preview = _run_detail_preview_unavailable(
                        "missing" if capture_complete else "not captured"
                    )
                    if input_ref:
                        pass_input_preview = _verification_blob_preview(
                            source_verification_id,
                            input_ref,
                        )
                    row_payload["input_preview"] = pass_input_preview
                    if interactive_mode:
                        transcript_rel = str(row_payload.get("transcript_rel") or "")
                        row_payload["interactive_transcript"] = _verification_transcript(
                            source_verification_id,
                            transcript_rel,
                            unavailable_message="missing"
                            if capture_complete
                            else "not captured",
                        )
                        judge_message_rel = str(row_payload.get("judge_message_rel") or "")
                        feedback_preview = _run_detail_preview_unavailable(
                            "missing" if capture_complete else "not captured"
                        )
                        if judge_message_rel:
                            feedback_preview = _verification_blob_preview(
                                source_verification_id,
                                judge_message_rel,
                            )
                            feedback_preview["download_verification_id"] = ""
                            feedback_preview["download_rel_path"] = ""
                        row_payload["feedback_preview"] = feedback_preview
                    else:
                        checker_log_rel = str(row_payload.get("checker_log_rel") or "")
                        feedback_rel = str(row_payload.get("feedback_rel") or "")
                        feedback_preview = _run_detail_preview_unavailable("missing")
                        if feedback_rel:
                            feedback_preview = _verification_blob_preview(
                                source_verification_id,
                                feedback_rel,
                            )
                        elif checker_log_rel:
                            feedback_preview = _verification_blob_preview(
                                source_verification_id,
                                checker_log_rel,
                            )
                        row_payload["feedback_preview"] = feedback_preview
                        if (row_payload.get("feedback_display") or "-") == "-" and bool(
                            feedback_preview.get("available")
                        ):
                            preview_text = (
                                str(feedback_preview.get("text") or "")
                                .replace("\r\n", "\n")
                                .replace("\r", "\n")
                            )
                            first_line = next(
                                (line for line in preview_text.splitlines() if line), ""
                            )
                            if first_line:
                                row_payload["feedback_display"] = (
                                    first_line[:157].rstrip() + "..."
                                    if len(first_line) > 160
                                    else first_line
                                )
                    pass_rows_payload.append(row_payload)
                detail_payload["pass_rows"] = pass_rows_payload
                detail_payload["is_interactive"] = interactive_mode
                detail_payload["mode_malformed"] = (col.get("mode") or "") == "malformed"
                final_row_payload = detail_payload["final_row"].copy()
                if pass_rows_payload:
                    final_row_payload = pass_rows_payload[-1].copy()
                    for candidate in reversed(pass_rows_payload):
                        verdict_token = candidate.get("verdict_short") or ""
                        if verdict_token and verdict_token not in {"--", "-"}:
                            final_row_payload = candidate.copy()
                            break
                detail_payload["final_row"] = final_row_payload
            detail_cells.append(
                {
                    "text": (cell["text"]),
                    "short": (cell.get("short") or cell.get("text") or "--"),
                    "metrics": (cell.get("metrics") or "-"),
                    "time_display": (cell.get("time_display") or ""),
                    "time_tone": (cell.get("time_tone") or ""),
                    "memory_display": (cell.get("memory_display") or ""),
                    "kind": (cell["kind"]),
                    "text_tone": (cell.get("text_tone") or ""),
                    "detail": detail_payload,
                }
            )
        if detail_is_main_correct_run:
            for detail_cell in detail_cells:
                main_detail_payload = detail_cell["detail"]
                if main_detail_payload is None:
                    continue
                final_output_preview = main_detail_payload["final_row"].get("output_preview")
                if final_output_preview is not None and final_output_preview["available"]:
                    answer_preview = final_output_preview
                    break
        generate_note = dict(row_generate_notes.get(test_name) or {})
        test_cell = _test_name_cell(
            actual_test_name=test_name,
            fallback_name=test_name,
            is_placeholder=False,
            note=generate_note,
            has_detail=bool(
                generation_terminal
                or any((cell.get("detail") is not None for cell in detail_cells))
            ),
        )
        detail_rows.append(
            {
                "index": row_index,
                "test_name": test_name,
                "display_name": test_name,
                "test_cell": test_cell,
                "is_placeholder": False,
                "row_id": f"test-detail-{row_index}",
                "input_preview": input_preview,
                "answer_preview": answer_preview,
                "is_interactive": row_is_interactive,
                "generate_detail": (
                    generation_view
                    if generation_view is not None
                    and generation_view["status"] == VerificationTaskStatus.FAILED
                    else None
                ),
                "generation_alert": generation_alert,
                "generation_skipped": bool(
                    generation_view is not None and generation_view["skipped"]
                ),
                "test_source_kind": ""
                if generation_view is None
                else generation_view["source_kind"],
                "test_command": "" if generation_view is None else generation_view["command"],
                "cells": detail_cells,
                "has_detail": bool(
                    generation_terminal
                    or any((cell.get("detail") is not None for cell in detail_cells))
                ),
            }
        )
    return detail_rows


def build_run_test_detail_context(
    ctx: WorkspaceContext, *, verification_id: str, test_name: str, program_id: str = "",
) -> RunTestDetailContext:
    """Project one authorized historical testcase without authoring side effects."""
    def authorize(record: VerificationSnapshotRecord) -> None:
        access = runtime().access_query.verification_context(
            actor_user_id=int(ctx["user"]["id"]),
            actor_workspace_id=int(ctx["workspace"]["id"]),
            expected_problem_id=int(ctx["problem"]["id"]), verification=record,
        )
        if not access["can_view"]:
            raise HTTPException(status_code=404, detail="run detail not found")

    model = runtime().verification_service.verification_test_detail_read_model(
        verification_id, test_name=test_name, program_id=program_id or None, authorize=authorize,
    )
    if model is None:
        raise HTTPException(status_code=404, detail="run detail not found")
    details = model["details"]
    display_limit = runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES")
    metadata = {item.get("test_name", ""): item for item in details.get("tests_meta_rows", [])}
    selected_metadata = {test_name: metadata[test_name]} if test_name in metadata else {}
    generation = build_test_generation_views(model["tasks"], selected_metadata, limit_bytes=display_limit)
    notes = {
        name: {"tone": view["tone"], "status_label": view["status_label"],
               "text": view["table_text"], "detail": view["alert_message"] or view["detail"]}
        for name, view in generation.items()
    }
    columns: list[RunColumnBase] = []
    for task in model["cases"]:
        tests_map: dict[str, RunCaseCell] = {}
        if task["status"] in {VerificationTaskStatus.DONE, VerificationTaskStatus.FAILED} and task["verdict"] != "SK":
            item = verification_case_test_row(task, display_limit=display_limit)
            feedback = {"tests": [item]}
            diagnostics = _decorate_compile_diagnostics(_normalize_diagnostics(
                compile_diagnostics_payload(task["result"].compile.diagnostics)[
                    :runtime().config_values.integer("RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT")
                ], runtime().config_values.integer("DIAGNOSTIC_MESSAGE_CHAR_LIMIT"),
            ))
            error = bounded_display_text(
                "\n\n".join(value for value in (task["error_text"], str(task.get("late_diagnostic_text") or "")) if value),
                limit_bytes=display_limit,
            )
            if not error and diagnostics:
                first = diagnostics[0]
                error = ": ".join(str(first.get(key) or "").strip() for key in ("location_display", "message") if first.get(key))
            threshold = evaluate_summary_runtime_threshold(
                summary=feedback, source=task["source_path"],
                time_limit_ms=time_limit_ms_from_run_config_json(str(details.get("run_config_json") or "")),
            )
            tests_map[test_name] = _case_cell(
                item, idx=1, test_name=test_name, expected_behavior=task["expected_behavior"],
                verification_details=details, include_row_details=True,
                detail_compile_error=error,
                detail_compile_diagnostics=diagnostics,
                display_limit=display_limit, time_tone="warn" if test_name in threshold.highlighted_tests else "",
            )
        columns.append({
            "id": task["program_id"], "title": Path(task["source_path"]).name or task["program_id"],
            "mode": model["mode"], "tests_map": tests_map,
        })
    known_test = test_name in metadata or any(task["test_name"] == test_name for task in model["tasks"])
    target_tests = [test_name] if known_test and (not program_id or columns) else []
    rows = _test_detail_rows(
        target_tests=target_tests, row_index_by_test={test_name: 1}, columns=columns,
        test_generation_views=generation, row_generate_notes=notes,
        source_verification_id=model["record"]["id"], problem_slug=ctx["problem"]["slug"],
        username=ctx["user"]["username"],
    )
    return {"verification_id": model["record"]["id"], "detail_columns": columns, "detail_rows": rows}


def build_run_detail_context(
    ctx: WorkspaceContext,
    *,
    requested_verification_id: str = "",
    include_row_details: bool = False,
    detail_test_name: str = "",
) -> RunDetailContext:
    display_limit = runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES")

    workspace = Path(ctx["workspace"]["path"])
    problem_id = int(ctx["problem"]["id"])
    workspace_id = int(ctx["workspace"]["id"])
    problem_slug = ctx["problem"]["slug"]
    username = ctx["user"]["username"]
    selected_program_ids: list[str] = []
    verification_program_rows: dict[str, VerificationProgramDetailRow] = {}
    verification_id_hint = normalize_run_id_token(requested_verification_id)
    verification_details: VerificationDetail = {}
    task_rows: list[VerificationTaskRow] = []
    has_task_graph = False
    source_verification_id = (
        verification_id_hint if is_canonical_artifact_id(verification_id_hint) else ""
    )
    verification_read_model = (
        runtime().verification_service.verification_detail_read_model(
            verification_id_hint, include_pass_details=include_row_details,
        )
        if verification_id_hint
        else None
    )
    verification_record = (
        verification_read_model["record"] if verification_read_model is not None else None
    )
    verification_access = runtime().access_query.verification_context(
        actor_user_id=int(ctx["user"]["id"]),
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verification=verification_record,
    )
    verification_visible = verification_access["can_view"]
    if verification_visible and verification_record is not None:
        assert verification_read_model is not None
        task_rows = verification_read_model["tasks"]
        has_task_graph = verification_read_model["has_task_graph"]
        verification_details = verification_read_model["details"]
        source_verification_id = str(
            verification_details.get("artifact_verification_id") or verification_id_hint or ""
        )
        if not is_canonical_artifact_id(source_verification_id):
            source_verification_id = ""
        if has_task_graph:
            verification_program_rows = verification_read_model["program_rows"]
            selected_program_ids = verification_read_model["program_ids"]
    runtime_threshold_time_limit_ms = time_limit_ms_from_run_config_json(
        str(verification_details.get("run_config_json") or ""),
        default_ms=0,
    )
    verification_created_at = ""
    if not verification_created_at and verification_record is not None:
        verification_created_at = verification_record["created_at"]
    preferred_solution_program_ids = (
        verification_read_model["program_ids"]
        if has_task_graph and verification_read_model is not None
        else []
    )
    if preferred_solution_program_ids:
        selected_program_ids = list(preferred_solution_program_ids)

    def _is_solution_column_source(source_value: str) -> bool:
        safe_source = normalize_optional_component_source_path_safe(
            source_value,
            "solutions",
            "solution path",
        )
        return bool(safe_source)

    columns: list[RunColumn] = []
    all_tests: set[str] = set()
    tests_meta_by_test_name: dict[str, VerificationTestMetadata] = {}
    for test_metadata in verification_details.get("tests_meta_rows", []):
        test_name = normalize_run_test_name_token(str(test_metadata.get("test_name") or ""))
        if test_name and test_name not in tests_meta_by_test_name:
            tests_meta_by_test_name[test_name] = test_metadata
    test_generation_views = build_test_generation_views(
        task_rows,
        tests_meta_by_test_name,
        limit_bytes=display_limit,
    )
    row_generate_notes: dict[str, dict[str, str]] = {
        test_name: {
            "tone": str(view["tone"]),
            "status_label": str(view["status_label"]),
            "text": str(view["table_text"]),
            "detail": str(view["alert_message"] or view["detail"]),
        }
        for test_name, view in test_generation_views.items()
    }
    task_graph_task_status_by_program_and_test: dict[tuple[str, str], str] = {}
    if has_task_graph and verification_read_model is not None:
        task_graph_task_status_by_program_and_test = verification_read_model[
            "task_status_by_program_and_test"
        ]
        for task_row in task_rows:
            test_name = normalize_run_test_name_token(
                str(task_row["test_name"] or "")
            )
            if test_name:
                all_tests.add(test_name)
    selected_test_name_hint = (
        normalize_run_test_name_token(detail_test_name) if include_row_details else ""
    )

    for program_id in selected_program_ids:
        program_row = verification_program_rows.get(program_id)
        if program_row is None:
            continue
        status = program_row["status"]
        mode = program_row["mode"]
        created_at = program_row["created_at"]
        finished_at = program_row["finished_at"]
        artifact_verification_id = program_row["artifact_verification_id"]
        summary = program_row["summary"].copy()
        source_label = program_row["source_label"]
        if mode not in {"pass-fail", "interactive"}:
            mode = "malformed"
        test_limit = max(1, runtime().config_values.integer("RUN_DETAIL_TEST_LIST_LIMIT"))
        tests_total = max(len(summary["tests"]), summary["tests_total"])
        summary["tests"] = summary["tests"][:test_limit]
        detail_compile_diagnostics = _decorate_compile_diagnostics(_normalize_diagnostics(
            summary["compile_diagnostics"][:runtime().config_values.integer("RUN_DETAIL_DIAGNOSTIC_LIST_LIMIT")],
            runtime().config_values.integer("DIAGNOSTIC_MESSAGE_CHAR_LIMIT"),
        ))
        detail_compile_error = bounded_display_text(
            _detail_text(summary.get("error"), field="program.error"),
            limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
        )
        if (not detail_compile_error) and detail_compile_diagnostics:
            first_diag = detail_compile_diagnostics[0]
            diag_location = str(first_diag.get("location_display") or "").strip()
            diag_message = str(first_diag.get("message") or "").strip()
            if diag_location and diag_message:
                detail_compile_error = f"{diag_location}: {diag_message}"
            elif diag_message:
                detail_compile_error = diag_message
        source = summary["source"]
        task_kind = _run_task_kind_from_summary(summary)
        is_main_correct_run = _is_main_correct_task_kind(task_kind)
        source_for_display = source or source_label
        title = Path(source_for_display).name if source_for_display else ""
        if not title:
            title = program_id or "unknown program"
        source_section = ""
        source_path = ""
        source_rel = normalize_workspace_rel_path(source_for_display)
        if (
            problem_slug
            and username
            and source_rel
            and workspace_rel_file_exists(workspace, source_rel)
        ):
            safe_solution = normalize_optional_component_source_path_safe(
                source_rel, "solutions", "solution path"
            )
            if safe_solution:
                source_section = "solutions"
                source_path = safe_solution
            else:
                source_section = "files"
                source_path = source_rel
        expected_behavior = _run_expected_behavior_from_summary(summary)
        result_analysis = analyze_program_result(status, summary)
        matched, completed, observed_pass, match_reason = result_analysis.match(expected_behavior)
        required_codes, allowed_codes = expected_status_rule(expected_behavior)
        expected_display = status_rule_expected_display(expected_behavior)
        expected_is_ac_only = bool(required_codes == ("AC",) and allowed_codes == ("AC",))
        got_short = result_analysis.short
        got_display = result_analysis.display
        result_kind = _run_result_kind(
            expected_behavior,
            matched=matched,
            completed=completed,
            observed_pass=observed_pass,
            got_short=got_short,
        )
        result_text_tone = ""
        result_tone_class = f"tone-{result_kind}"
        expected_mismatch = bool(expected_behavior != "unknown" and completed and (not matched))
        execution_skipped_from_summary = bool(summary.get("execution_skipped"))
        if not execution_skipped_from_summary and (summary.get("failure_stage") or "") == "build":
            execution_skipped_from_summary = True
        tests_map: dict[str, RunCaseCell] = {}
        max_time_ms = 0
        max_time_tone = ""
        max_memory_kb = 0
        has_test_metrics = False
        tests_raw = summary["tests"]
        runtime_threshold_report = evaluate_summary_runtime_threshold(
            summary=summary,
            source=source_for_display,
            time_limit_ms=runtime_threshold_time_limit_ms,
        )
        has_materialized_tests = bool(tests_raw)
        for idx, item in enumerate(tests_raw, start=1):
            test_name = _detail_text(
                item.get("test"),
                field=f"program.tests[{idx - 1}].test",
                default=str(idx),
            )
            if not test_name:
                continue
            if selected_test_name_hint and test_name != selected_test_name_hint:
                continue
            time_tone = "warn" if test_name in runtime_threshold_report.highlighted_tests else ""
            projected_cell = _case_cell(
                item, idx=idx, test_name=test_name, expected_behavior=expected_behavior,
                verification_details=verification_details, include_row_details=include_row_details,
                detail_compile_error=detail_compile_error,
                detail_compile_diagnostics=detail_compile_diagnostics,
                display_limit=display_limit, time_tone=time_tone,
            )
            has_test_metrics = True
            if projected_cell["time_ms"] > max_time_ms:
                max_time_ms = projected_cell["time_ms"]
                max_time_tone = time_tone
            max_memory_kb = max(max_memory_kb, projected_cell["memory_kb"])
            all_tests.add(test_name)
            tests_map[test_name] = projected_cell
        execution_skipped = bool(execution_skipped_from_summary and (not has_materialized_tests))
        execution_skipped_reason = bounded_display_text(
            _detail_text(
                summary.get("execution_skipped_reason") or summary.get("error"),
                field="program.execution_skipped_reason",
            ),
            limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
        )
        max_time_display = f"{max_time_ms}ms" if has_test_metrics else "-"
        max_memory_display = run_memory_mb_text(max_memory_kb) if has_test_metrics else "-"
        failure_display = (
            verification_solution_failure_hint(
                source_for_display,
                match_reason,
                str(summary.get("error") or ""),
                limit_bytes=runtime().config_values.integer(
                    "AUX_DISPLAY_TEXT_LIMIT_BYTES"
                ),
            )
            if (match_reason or summary.get("error"))
            else ""
        )
        column_payload: RunColumn = {
            "id": program_id,
            "artifact_verification_id": artifact_verification_id,
            "title": title,
            "source": source_for_display or "-",
            "source_section": source_section,
            "source_path": source_path,
            "task_kind": task_kind,
            "is_main_correct_run": bool(is_main_correct_run),
            "status": status,
            "mode": mode,
            "created_at": created_at,
            "finished_at": finished_at,
            "tests_map": tests_map,
            "compile_diagnostics": detail_compile_diagnostics,
            "error": _detail_text(summary.get("error"), field="program.error"),
            "error_display": run_error_display(
                _detail_text(summary.get("error"), field="program.error")
            ),
            "tests_total": tests_total,
            "expected_behavior": expected_behavior,
            "expected_display": expected_display,
            "expected_is_ac_only": bool(expected_is_ac_only),
            "got_short": got_short,
            "got_display": got_display,
            "result_kind": result_kind,
            "result_text_tone": result_text_tone,
            "result_tone_class": result_tone_class,
            "expected_mismatch": bool(expected_mismatch),
            "matched": bool(matched),
            "completed": bool(completed),
            "passed_all_tests": bool(observed_pass),
            "match_reason": (match_reason or ""),
            "execution_skipped": bool(execution_skipped),
            "execution_skipped_reason": execution_skipped_reason,
            "max_time_ms": int(max_time_ms),
            "max_time_display": max_time_display,
            "max_time_tone": max_time_tone,
            "max_memory_kb": int(max_memory_kb),
            "max_memory_display": max_memory_display,
            "failure_display": failure_display,
        }
        if not _is_solution_column_source(source_for_display) and task_kind not in {
            _TASK_KIND_SOLUTION_RUN,
            _TASK_KIND_MAIN_CORRECT,
        }:
            continue
        columns.append(column_payload)
    status_summary = _verification_status_summary(columns)
    if verification_details:
        overall_status = (
            verification_details.get("status")
            or (verification_record["status"] if verification_record is not None else "")
            or ""
        )
        if overall_status in {"running", "queued", "pending"}:
            status_summary = {
                "status": "running",
                "is_failed": False,
                "has_running": True,
                "matched_count": status_summary["matched_count"],
                "total_count": status_summary["total_count"],
            }
        elif overall_status == "failed":
            status_summary = {
                "status": "failed",
                "is_failed": True,
                "has_running": False,
                "matched_count": status_summary["matched_count"],
                "total_count": status_summary["total_count"],
            }
    if (not columns) and verification_details:
        fallback_status = (
            verification_details.get("status")
            or (verification_record["status"] if verification_record is not None else "")
            or ""
        )
        fallback_total = len(
            _detail_string_list(
                verification_details.get("source_paths"),
                field="source_paths",
            )
        )
        if fallback_status in {"running", "queued", "pending"}:
            status_summary = {
                "status": "running",
                "is_failed": False,
                "has_running": True,
                "matched_count": 0,
                "total_count": fallback_total,
            }
        elif fallback_status == "failed":
            status_summary = {
                "status": "failed",
                "is_failed": True,
                "has_running": False,
                "matched_count": 0,
                "total_count": fallback_total,
            }
        elif fallback_status in {"ok", "pass"}:
            status_summary = {
                "status": "ok",
                "is_failed": False,
                "has_running": False,
                "matched_count": fallback_total,
                "total_count": fallback_total,
            }
    detail_task_kinds = {(col.get("task_kind") or "") for col in columns if col.get("task_kind")}
    detail_is_main_correct_run = bool(detail_task_kinds) and detail_task_kinds.issubset(
        {"main-correct"}
    )
    if has_task_graph:
        detail_is_main_correct_run = False
    ordered_tests = sorted(all_tests, key=_run_test_sort_key)
    generation_diagnostic_message = generation_warning_message(test_generation_views, ordered_tests)
    known_tests_by_index: dict[int, str] = {}
    for test_name in ordered_tests:
        try:
            test_index = int(Path(test_name).stem)
        except Exception:
            continue
        if test_index > 0 and test_index not in known_tests_by_index:
            known_tests_by_index[test_index] = test_name
    tests_meta_stats = _verification_tests_meta_stats(verification_details)
    try:
        tests_meta_total = max(0, int(tests_meta_stats.get("total") or 0))
    except Exception:
        tests_meta_total = 0
    column_tests_total = 0
    for col in columns:
        try:
            column_tests_total = max(column_tests_total, int(col.get("tests_total") or 0))
        except Exception:
            continue
    display_test_total = max(
        max(known_tests_by_index.keys(), default=0), tests_meta_total, column_tests_total
    )
    row_index_by_test = {name: idx for idx, name in enumerate(ordered_tests, start=1)}
    detail_rows: list[RunTestRow] = []
    if not include_row_details:
        row_entries: list[tuple[int, str, str, bool]] = []
        if bool(status_summary["has_running"]) and display_test_total > 0:
            for idx in range(1, display_test_total + 1):
                actual_name = known_tests_by_index.get(idx) or ""
                display_name = actual_name or f"{idx:03d}.in"
                row_entries.append((idx, actual_name, display_name, not bool(actual_name)))
        else:
            row_entries = [
                (idx, test_name, test_name, False)
                for idx, test_name in enumerate(ordered_tests, start=1)
            ]
        for idx, actual_test_name, display_name, is_placeholder in row_entries:
            cells: list[RunCellView] = []
            has_detail = False
            generation_view = (
                test_generation_views.get(actual_test_name) if actual_test_name else None
            )
            generation_terminal = bool(generation_view is not None and generation_view["terminal"])
            generation_skipped = bool(generation_view is not None and generation_view["skipped"])
            if not generation_skipped:
                for col in columns:
                    cell = col["tests_map"].get(actual_test_name) if actual_test_name else None
                    if cell is None:
                        if has_task_graph and actual_test_name:
                            task_status = task_graph_task_status_by_program_and_test.get(
                                (str(col.get("id") or ""), actual_test_name), ""
                            )
                            cells.append(_missing_solution_cell(task_status))
                        else:
                            col_status = col.get("status") or ""
                            missing_running = col_status == "running"
                            missing_pending = col_status in {"queued", "pending"}
                            cells.append(
                                {
                                    "text": ".." if (missing_running or missing_pending) else "--",
                                    "short": ".." if (missing_running or missing_pending) else "--",
                                    "metrics": "running"
                                    if missing_running
                                    else ""
                                    if missing_pending
                                    else "-",
                                    "kind": "running" if missing_running else "neutral",
                                    "text_tone": "",
                                    "detail": None,
                                }
                            )
                        continue
                    if bool(cell.get("detail_available")):
                        has_detail = True
                    cells.append(
                        {
                            "text": (cell.get("text") or "--"),
                            "short": (cell.get("short") or cell.get("text") or "--"),
                            "metrics": (cell.get("metrics") or "-"),
                            "time_display": (cell.get("time_display") or ""),
                            "time_tone": (cell.get("time_tone") or ""),
                            "memory_display": (cell.get("memory_display") or ""),
                            "kind": (cell.get("kind") or "neutral"),
                            "text_tone": (cell.get("text_tone") or ""),
                            "detail": None,
                        }
                    )
            generate_note = dict(row_generate_notes.get(actual_test_name or display_name) or {})
            test_cell = _test_name_cell(
                actual_test_name=actual_test_name,
                fallback_name=display_name,
                is_placeholder=bool(is_placeholder),
                note=generate_note,
                has_detail=bool(has_detail or generation_terminal),
            )
            detail_rows.append(
                {
                    "index": idx,
                    "test_name": actual_test_name or display_name,
                    "display_name": display_name,
                    "test_cell": test_cell,
                    "is_placeholder": bool(is_placeholder),
                    "row_id": f"test-detail-{idx}",
                    "cells": cells,
                    "has_detail": bool(
                        (has_detail or generation_terminal) and (not is_placeholder)
                    ),
                    "test_source_kind": ""
                    if generation_view is None
                    else generation_view["source_kind"],
                    "test_command": "" if generation_view is None else generation_view["command"],
                    "generation_skipped": generation_skipped,
                    "generation_message": ""
                    if generation_view is None
                    else generation_view["alert_message"],
                }
            )
    else:
        selected_test_name = selected_test_name_hint
        target_tests = ordered_tests
        if selected_test_name:
            target_tests = [name for name in ordered_tests if name == selected_test_name]

        source_verification_id = str(
            verification_details.get("artifact_verification_id") or verification_id_hint or ""
        )
        if not is_canonical_artifact_id(source_verification_id):
            source_verification_id = ""

        detail_rows = _test_detail_rows(
            target_tests=target_tests, row_index_by_test=row_index_by_test, columns=columns,
            test_generation_views=test_generation_views, row_generate_notes=row_generate_notes,
            source_verification_id=source_verification_id, problem_slug=problem_slug,
            username=username, detail_is_main_correct_run=detail_is_main_correct_run,
        )
    rejudge_context = _run_rejudge_context_for_entries(columns, workspace)
    rerun_paths = rejudge_context.get("paths") or []
    progress_total = 0
    for col in columns:
        if bool(col.get("execution_skipped")):
            continue
        try:
            progress_total = max(progress_total, int(col.get("tests_total") or 0))
        except Exception:
            continue
    progress_placeholder_total = (
        min(progress_total, 24) if bool(status_summary["has_running"]) and progress_total > 0 else 0
    )
    last_updated_candidates: list[str] = [(col.get("finished_at") or "") for col in columns]
    last_updated_candidates.extend([(col.get("created_at") or "") for col in columns])
    last_updated_candidates.append(
        _detail_text(verification_details.get("updated_at"), field="updated_at")
    )
    last_updated_candidates.append(
        _detail_text(verification_details.get("finished_at"), field="finished_at")
    )
    if verification_created_at:
        last_updated_candidates.append(verification_created_at)
    last_updated = _latest_iso_timestamp(last_updated_candidates)
    verification_id = verification_id_hint if verification_visible else ""
    if has_task_graph and verification_read_model is not None:
        task_counts = verification_read_model["task_counts"]
        running_tasks = verification_read_model["running_tasks"]
    else:
        task_counts = app.service.verification.read_model.task_counts([])
        running_tasks = []
    detail_fail_reason = str(
        (verification_record.get("fail_reason") if verification_record is not None else "") or ""
    )
    detail_fail_flag = bool(detail_fail_reason)
    detail_fail_reason = rewrite_failure_reason_with_source(
        detail_fail_reason,
        columns,
        limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
    )
    detail_fail_flag = bool(detail_fail_reason)
    detail_sanity = _detail_sanity_context(verification_id, verification_details)
    detail_status = str(status_summary["status"])
    detail_sanity_status = str(detail_sanity.get("status") or "")
    detail_status_display = detail_status
    if detail_status == "ok" and detail_sanity_status == "warning":
        detail_status_display = "ok (has warning)"
    elif detail_status == "ok" and detail_sanity_status == "failed":
        detail_status_display = "ok (sanity failed)"
    detail_status_tone = (
        "warn"
        if detail_status == "ok" and detail_sanity_status in {"warning", "failed"}
        else detail_status
    )
    verification_logs: RunVerificationLogs = {
        "available": False,
        "title": "Verification",
        "verification_id": "",
        "status": "",
        "error": "",
        "error_display": "",
        "diagnostics": [],
    }

    if source_verification_id and problem_slug and username:
        artifact_verification_status = (
            verification_record["status"] if verification_record is not None else ""
        )
        artifact_verification_error = detail_fail_reason
        diagnostics_title = "Verification"
        diagnostics_rows: list[DiagnosticEntry] = []
        for col in columns:
            raw_diags = col["compile_diagnostics"]
            if not raw_diags:
                continue
            diagnostics_rows = raw_diags
            diagnostics_title = str(col.get("title") or "Verification")
            break
        if diagnostics_rows and (
            (not artifact_verification_error)
            or ("/opt/domjudge/judgehost/judgings/" in artifact_verification_error)
        ):
            first_verification_diag = diagnostics_rows[0]
            diag_location = first_verification_diag.get("location_display") or ""
            diag_message = first_verification_diag.get("message") or ""
            if diag_location and diag_message:
                artifact_verification_error = f"{diag_location}: {diag_message}"
            elif diag_message:
                artifact_verification_error = diag_message
        source_aware_column_reason = rewrite_failure_reason_with_source(
            "",
            columns,
            limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
        )
        artifact_verification_error = rewrite_failure_reason_with_source(
            artifact_verification_error,
            columns,
            limit_bytes=runtime().config_values.integer("AUX_DISPLAY_TEXT_LIMIT_BYTES"),
        )
        generic_column_reasons = {
            str(col.get("match_reason") or "").strip()
            for col in columns
            if str(col.get("match_reason") or "").strip()
        }
        if (not diagnostics_rows) and (
            artifact_verification_error in generic_column_reasons
            or (
                source_aware_column_reason
                and artifact_verification_error == source_aware_column_reason
            )
        ):
            artifact_verification_error = ""
        verification_logs = {
            "available": True,
            "title": diagnostics_title,
            "verification_id": source_verification_id,
            "status": artifact_verification_status,
            "error": artifact_verification_error,
            "error_display": run_error_display(artifact_verification_error),
            "diagnostics": diagnostics_rows,
        }

    return {
        "verification_id": verification_id,
        "can_rejudge": verification_access["can_rejudge"],
        "can_cancel": verification_access["can_cancel"],
        "detail_columns": columns,
        "detail_rows": detail_rows,
        "selected_program_ids": selected_program_ids,
        "rerun_solution_paths": rerun_paths,
        "rerun_unavailable_reason": (rejudge_context.get("unavailable_reason") or ""),
        "matched_count": int(status_summary["matched_count"]),
        "match_total": int(status_summary["total_count"]),
        "all_matched": bool(columns) and all((bool(col.get("matched")) for col in columns)),
        "detail_status": detail_status,
        "detail_status_display": detail_status_display,
        "detail_status_tone": detail_status_tone,
        "detail_is_main_correct_run": bool(detail_is_main_correct_run),
        "detail_running": bool(status_summary["has_running"]),
        "detail_last_updated": last_updated,
        "detail_progress_total": progress_total,
        "detail_progress_placeholder_total": progress_placeholder_total,
        "detail_task_counts": task_counts,
        "detail_running_tasks": running_tasks,
        "detail_fail_flag": detail_fail_flag,
        "detail_fail_reason": detail_fail_reason,
        "detail_sanity": detail_sanity,
        "detail_generation_diagnostic": {
            "title": "Test generation",
            "message": generation_diagnostic_message,
        }
        if generation_diagnostic_message
        else None,
        "detail_verification_logs": verification_logs,
    }
