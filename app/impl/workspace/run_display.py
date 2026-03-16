from __future__ import annotations


_COMPILE_ERROR_VALUES = {"compile_error", "compile error", "ce"}


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
    if verdict in {"", "-"}:
        return "--"
    return "FL"


def run_error_display(error: str) -> str:
    if _is_compile_error(error):
        return "CE"
    return error


def run_actual_failed_codes(run_status: str, summary: dict | None) -> list[str]:
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
            if code not in {"", "--", "AC"}:
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
    return f"{safe_cpu_ms}ms cpu, {safe_wall_ms}ms wall"
