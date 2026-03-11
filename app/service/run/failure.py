from __future__ import annotations

import json
from pathlib import Path

from .runtime import RUN_TEST_NAME_RE
from .summary import compact_inline_error


def build_failure_context(build_row: object) -> tuple[str, str]:
    if build_row is None:
        return ("", "")
    status = ""
    summary_raw = ""
    try:
        status = str(build_row["status"] or "").strip().lower()
    except Exception:
        status = ""
    try:
        summary_raw = str(build_row["summary_json"] or "")
    except Exception:
        summary_raw = ""
    summary_obj: dict = {}
    if summary_raw:
        try:
            parsed = json.loads(summary_raw)
            if isinstance(parsed, dict):
                summary_obj = parsed
        except Exception:
            summary_obj = {}
    failed_test_raw = str(summary_obj.get("failed_test") or "").strip()
    failed_test = ""
    if failed_test_raw:
        test_name = Path(failed_test_raw).name
        if RUN_TEST_NAME_RE.fullmatch(test_name):
            failed_test = test_name
    failed_step = str(summary_obj.get("failed_step") or "").strip()
    build_error = compact_inline_error(summary_obj.get("error"))
    reason = ""
    if build_error:
        reason = build_error
    elif failed_step and failed_test_raw:
        reason = compact_inline_error(f"{failed_step} failed on {failed_test_raw}")
    elif failed_step:
        reason = compact_inline_error(f"{failed_step} failed")
    elif status and status != "ok":
        reason = f"build status is {status}"
    return (failed_test, reason)


def synthesize_failure_tests(
    *,
    preferred_test: str = "",
    selected_test_names: list[str] | None = None,
    reason: str = "",
) -> list[dict]:
    candidates: list[str] = []
    if preferred_test:
        candidates.append(preferred_test)
    for item in selected_test_names or []:
        candidates.append(str(item or ""))
    candidates.append("001.in")
    test_name = ""
    for raw in candidates:
        token = Path(str(raw or "").strip()).name
        if RUN_TEST_NAME_RE.fullmatch(token):
            test_name = token
            break
    if not test_name:
        return []
    feedback = compact_inline_error(reason)
    pass_row: dict[str, object] = {
        "pass": 1,
        "verdict": "FL",
        "time_ms": 0,
        "time_user_ms": 0,
        "time_wall_ms": 0,
        "memory_kb": 0,
    }
    if feedback:
        pass_row["feedback"] = feedback
    test_row: dict[str, object] = {
        "test": test_name,
        "passes": [pass_row],
        "verdict": "FL",
        "sandbox_status": "fail",
        "time_ms": 0,
        "time_user_ms": 0,
        "time_wall_ms": 0,
        "memory_kb": 0,
        "feedback_files": [],
    }
    if feedback:
        test_row["message"] = feedback
    return [test_row]


def is_fl_verdict(verdict: object) -> bool:
    token = str(verdict or "").strip().upper()
    return token in {"FL", "FAIL", "FAILED"}


def synthesized_fl_skip_test(test_name: str, *, caused_by_test: str) -> dict[str, object]:
    reason = f"fail due to test {caused_by_test}"
    pass_row: dict[str, object] = {
        "pass": 1,
        "verdict": "FL",
        "time_ms": 0,
        "time_user_ms": 0,
        "time_wall_ms": 0,
        "memory_kb": 0,
        "feedback": reason,
    }
    return {
        "test": test_name,
        "passes": [pass_row],
        "verdict": "FL",
        "sandbox_status": "fail",
        "time_ms": 0,
        "time_user_ms": 0,
        "time_wall_ms": 0,
        "memory_kb": 0,
        "feedback_files": [],
        "message": reason,
    }


def append_fl_skip_tail_tests(
    *,
    verdicts: list[dict],
    test_meta: list[tuple[str, str]],
    failed_index: int,
    caused_by_test: str,
) -> None:
    for rem_name, _rem_stem in test_meta[failed_index + 1 :]:
        verdicts.append(synthesized_fl_skip_test(rem_name, caused_by_test=caused_by_test))


