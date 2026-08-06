from __future__ import annotations

from pathlib import Path

from app.service.platform.error_text import bounded_display_text


_COMPILE_ERROR_VALUES = {"compile_error", "compile error", "ce"}
_TRANSIENT_REASON_TOKENS = {"running", "queued", "pending"}


def _is_compile_error(error: str) -> bool:
    return error in _COMPILE_ERROR_VALUES


def run_verdict_short(verdict: str) -> str:
    if verdict in {"OK", "AC"}:
        return "AC"
    if verdict in {"CE", "COMPILE_ERROR", "COMPILE ERROR"}:
        return "CE"
    if verdict == "WA":
        return "WA"
    if verdict == "TL" or verdict.startswith("TL"):
        return "TL"
    if verdict == "RE":
        return "RE"
    if verdict in {"FAIL", "FAILED", "FL"}:
        return "FL"
    if verdict == "SK":
        return "SK"
    if verdict in {"", "-", "--"}:
        return "--"
    return "FL"


def run_error_display(error: str) -> str:
    if _is_compile_error(error):
        return "CE"
    return error


def run_actual_failed_codes(run_status: str, summary: dict | None) -> list[str]:
    if run_status == "cancelled":
        return []
    if run_status in {"running", "queued", "pending"}:
        return []
    if summary is not None:
        error = summary.get("error")
        if error is not None and _is_compile_error(error):
            return ["CE"]
        tests = summary.get("tests")
    else:
        tests = None
    verdicts: list[str] = []
    if tests is not None:
        for row in tests:
            code = run_verdict_short(row["verdict"])
            if code not in {"", "--", "AC", "SK"}:
                verdicts.append(code)
    if verdicts:
        priority = {"CE": 0, "TL": 1, "RE": 2, "WA": 3, "FL": 4}
        return sorted(set(verdicts), key=lambda code: (priority.get(code, 99), code))
    if run_status == "ok":
        return []
    return ["FL"]


def run_actual_short(run_status: str, summary: dict | None) -> str:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return failed_codes[0]
    if run_status == "cancelled":
        return "--"
    if run_status in {"running", "queued", "pending"}:
        return "--"
    return "AC"


def run_actual_display(run_status: str, summary: dict | None) -> str:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return "/".join(failed_codes)
    return run_actual_short(run_status, summary)


def run_memory_mb_text(memory_kb: int) -> str:
    mb = (max(0, memory_kb) + 1023) // 1024
    return f"{mb}MB"


def run_cpu_wall_ms_text(cpu_ms: int, wall_ms: int) -> str:
    safe_cpu_ms = max(0, cpu_ms)
    safe_wall_ms = max(0, wall_ms)
    return f"{safe_cpu_ms}ms ({safe_wall_ms}ms wall)"


def verification_solution_failure_hint(source_path: str, reason: str, error_text: str = "") -> str:
    source_label = Path(source_path.strip()).name if source_path.strip() else ""
    if not source_label:
        source_label = "solution"
    rich_error = bounded_display_text(error_text)
    if reason and rich_error:
        detail = f"{reason}: {rich_error}"
    elif reason:
        detail = reason
    elif rich_error:
        detail = rich_error
    else:
        detail = "verification mismatch"
    return bounded_display_text(f"{source_label}: {detail}")


def rewrite_failure_reason_with_source(
    current_reason: str,
    columns: list[dict[str, object]],
) -> str:
    source_reason = ""
    generic_match_reasons: set[str] = set()
    generic_error_texts: set[str] = set()
    for column in columns:
        source_path = str(column.get("source") or "")
        match_reason = str(column.get("match_reason") or "")
        error_text = str(column.get("error") or "")
        if match_reason:
            generic_match_reasons.add(match_reason.strip())
        if error_text:
            generic_error_texts.add(error_text.strip())
        if not (match_reason or error_text):
            continue
        if (not error_text) and match_reason in _TRANSIENT_REASON_TOKENS:
            continue
        source_reason = verification_solution_failure_hint(source_path, match_reason, error_text)
        if source_reason:
            break
    if not source_reason:
        return current_reason
    if not current_reason:
        return source_reason
    if current_reason in generic_match_reasons or current_reason in generic_error_texts:
        return source_reason
    if current_reason in {
        "verification failed",
        "solution run did not complete",
        "verification mismatch",
    }:
        return source_reason
    if current_reason.startswith("required=[") and ", allowed=[" in current_reason and ", got=[" in current_reason:
        return source_reason
    return current_reason


def generation_status_text(status: str, verdict: str) -> str:
    verdict_token = verdict.upper()
    if status == "leased":
        return "running"
    if status in {"queued", "pending"}:
        return "pending"
    if status == "cancelled":
        return "cancelled"
    if verdict_token in {"OK", "AC"}:
        return "OK"
    if verdict_token == "WA":
        return "validation failed"
    if verdict_token.startswith("TL"):
        return "generator TL"
    if verdict_token == "RE":
        return "generator RE"
    if verdict_token in {"CE", "COMPILE_ERROR", "COMPILE ERROR"}:
        return "generator CE"
    if verdict_token == "SK":
        return "skipped"
    if verdict_token in {"FL", "FAIL", "FAILED"}:
        return "validator failed"
    if status == "done":
        return "OK"
    if status == "failed":
        return verdict_token or "FL"
    return status or "-"
