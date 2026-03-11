from __future__ import annotations


def run_verdict_short(verdict: str) -> str:
    value = str(verdict or "").strip().upper()
    if value in {"OK", "AC"}:
        return "AC"
    if value in {"CE", "COMPILE_ERROR", "COMPILE ERROR"}:
        return "CE"
    if value == "WA":
        return "WA"
    if value == "TL" or value.startswith("TL"):
        return "TL"
    if value == "RE":
        return "RE"
    if value in {"FAIL", "FAILED", "FL"}:
        return "FL"
    if value in {"", "-"}:
        return "--"
    return "FL"


def run_error_display(error: str) -> str:
    raw = str(error or "").strip()
    code = raw.lower()
    if code in {"compile_error", "compile error", "ce"}:
        return "CE"
    return raw


def run_actual_failed_codes(run_status: str, summary: dict | None) -> list[str]:
    status = str(run_status or "").strip().lower()
    if status in {"running", "queued", "pending"}:
        return []
    error_code = str(summary.get("error") or "").strip().lower() if isinstance(summary, dict) else ""
    if error_code in {"compile_error", "compile error", "ce"}:
        return ["CE"]
    tests = summary.get("tests") if isinstance(summary, dict) else None
    verdicts: list[str] = []
    if isinstance(tests, list):
        for row in tests:
            if not isinstance(row, dict):
                continue
            code = run_verdict_short(str(row.get("verdict") or ""))
            if code in {"", "--", "AC"}:
                continue
            verdicts.append(code)
    if verdicts:
        priority = {"CE": 0, "TL": 1, "RE": 2, "WA": 3, "FL": 4}
        ordered = sorted(set(verdicts), key=lambda code: (priority.get(code, 99), str(code)))
        return [str(code) for code in ordered]
    if status == "ok":
        return []
    return ["FL"]


def run_actual_short(run_status: str, summary: dict | None) -> str:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return str(failed_codes[0] or "FL")
    status = str(run_status or "").strip().lower()
    if status in {"running", "queued", "pending"}:
        return "--"
    return "AC"


def run_actual_display(run_status: str, summary: dict | None) -> str:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return "/".join(failed_codes)
    return run_actual_short(run_status, summary)


def run_memory_mb_text(memory_kb: int) -> str:
    try:
        kb = max(0, int(memory_kb))
    except Exception:
        kb = 0
    mb = (kb + 1023) // 1024
    return f"{mb}MB"


def run_cpu_wall_ms_text(cpu_ms: int, wall_ms: int) -> str:
    try:
        safe_cpu_ms = max(0, int(cpu_ms))
    except Exception:
        safe_cpu_ms = 0
    try:
        safe_wall_ms = max(0, int(wall_ms))
    except Exception:
        safe_wall_ms = safe_cpu_ms
    return f"{safe_cpu_ms}ms cpu, {safe_wall_ms}ms wall"


