from __future__ import annotations

from app.service.problem.solution_metadata import normalize_expected_behavior


_COMPILE_ERROR_VALUES = {"compile_error", "compile error", "ce"}
_EXPECTED_STATUS_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "accepted": {"required": ("AC",), "allowed": ("AC",)},
    "wrong_answer": {"required": ("WA",), "allowed": ("AC", "WA")},
    "tle_or_correct": {"required": (), "allowed": ("AC", "TL")},
    "tle_or_re": {"required": (), "allowed": ("TL", "RE")},
    "time_limit_exceeded": {"required": ("TL",), "allowed": ("AC", "TL")},
    "run_time_error": {"required": ("RE",), "allowed": ("AC", "RE")},
    "rejected": {
        "required": ("WA", "TL", "RE", "CE"),
        "allowed": ("AC", "WA", "TL", "RE", "CE"),
    },
    "unknown": {"required": (), "allowed": ("AC", "WA", "TL", "RE", "CE")},
}


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(token for token in values if token))


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


def run_actual_failed_codes(
    run_status: str,
    summary: dict[str, object] | None,
) -> list[str]:
    if run_status == "cancelled" or run_status in {"running", "queued", "pending"}:
        return []
    tests: object = None
    if summary is not None:
        error = summary.get("error")
        if error in _COMPILE_ERROR_VALUES:
            return ["CE"]
        tests = summary.get("tests")
    verdicts: list[str] = []
    if isinstance(tests, list):
        for item in tests:
            if not isinstance(item, dict):
                continue
            code = run_verdict_short(str(item.get("verdict") or ""))
            if code not in {"", "--", "AC", "SK"}:
                verdicts.append(code)
    if verdicts:
        priority = {"CE": 0, "TL": 1, "RE": 2, "WA": 3, "FL": 4}
        return sorted(set(verdicts), key=lambda code: (priority.get(code, 99), code))
    return [] if run_status == "ok" else ["FL"]


def run_actual_short(
    run_status: str,
    summary: dict[str, object] | None,
) -> str:
    failed_codes = run_actual_failed_codes(run_status, summary)
    if failed_codes:
        return failed_codes[0]
    if run_status == "cancelled" or run_status in {"running", "queued", "pending"}:
        return "--"
    return "AC"


def expected_status_rule(
    expected_behavior: str,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rule = _EXPECTED_STATUS_RULES.get(
        normalize_expected_behavior(expected_behavior),
        _EXPECTED_STATUS_RULES["unknown"],
    )
    return (tuple(_dedupe(rule["required"])), tuple(_dedupe(rule["allowed"])))


def _status_codes_display(codes: list[str] | tuple[str, ...]) -> str:
    return "[" + ", ".join(_dedupe(codes)) + "]"


def status_rule_expected_display(expected_behavior: str) -> str:
    required_codes, allowed_codes = expected_status_rule(expected_behavior)
    display_codes = _dedupe(required_codes if required_codes else allowed_codes)
    if not display_codes:
        return "--"
    all_codes = _dedupe(("AC", "WA", "TL", "RE", "CE"))
    if display_codes == all_codes:
        return "any"
    missing_codes = [code for code in all_codes if code not in display_codes]
    if len(display_codes) == len(all_codes) - 1 and len(missing_codes) == 1:
        return f"not {missing_codes[0]}"
    return "/".join(display_codes)


def _status_rule_match(
    expected_behavior: str,
    run_status: str,
    summary: dict[str, object] | None,
) -> tuple[bool, str]:
    required_codes, allowed_codes = expected_status_rule(expected_behavior)
    observed_codes = run_actual_failed_codes(run_status, summary)
    if not observed_codes:
        token = run_actual_short(run_status, summary)
        observed_codes = [] if token in {"", "-", "--"} else [token]
    observed = set(observed_codes)
    required = set(required_codes)
    allowed = set(allowed_codes)
    matched = bool((not required or observed & required) and observed.issubset(allowed))
    if matched:
        return (True, "")
    return (
        False,
        f"required={_status_codes_display(required_codes)}, "
        f"allowed={_status_codes_display(allowed_codes)}, "
        f"got={_status_codes_display(observed_codes)}",
    )


def _run_completed(
    run_status: str,
    summary: dict[str, object] | None,
) -> bool:
    return bool(
        run_status == "ok"
        and summary is not None
        and not summary.get("error")
        and summary.get("tests")
    )


def _run_passed(
    run_status: str,
    summary: dict[str, object] | None,
) -> bool:
    if not _run_completed(run_status, summary) or summary is None:
        return False
    tests = summary.get("tests")
    if not isinstance(tests, list):
        return False
    return all(
        isinstance(item, dict) and item.get("verdict") == "OK"
        for item in tests
    )


def verification_solution_match(
    expected_behavior: str,
    run_status: str,
    summary: dict[str, object] | None,
) -> tuple[bool, bool, bool, str]:
    if run_status in {"running", "queued", "pending"}:
        return (False, False, False, "running")
    completed = _run_completed(run_status, summary)
    observed_pass = _run_passed(run_status, summary)
    if not completed:
        return (False, False, observed_pass, "")
    matched, reason = _status_rule_match(expected_behavior, run_status, summary)
    if matched:
        return (True, True, observed_pass, "")
    return (False, True, observed_pass, reason or "verification mismatch")
