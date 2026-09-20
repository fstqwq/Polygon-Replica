from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.service.execution.model import ExecutionResult
from app.service.problem.solution_metadata import normalize_expected_behavior


_COMPILE_ERROR_VALUES = {"compile_error", "compile error", "ce"}
_CANONICAL_DECISION_CODES = frozenset(("AC", "WA", "TL", "RE", "CE"))
_TRANSIENT_RUN_STATUSES = frozenset(("running", "queued", "pending"))
_EXPECTED_STATUS_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "accepted": {"required": ("AC",), "allowed": ("AC",)},
    "wrong_answer": {"required": ("WA",), "allowed": ("AC", "WA")},
    "tle_or_correct": {"required": (), "allowed": ("AC", "TL")},
    "tle_or_re": {"required": ("TL", "RE"), "allowed": ("AC", "TL", "RE")},
    "time_limit_exceeded": {"required": ("TL",), "allowed": ("AC", "TL")},
    "run_time_error": {"required": ("RE",), "allowed": ("AC", "RE")},
    "compile_error": {"required": ("CE",), "allowed": ("CE",)},
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


@dataclass(frozen=True)
class ProgramResultAnalysis:
    status: str
    completed: bool
    passed: bool
    failed_codes: tuple[str, ...]

    @property
    def short(self) -> str:
        if self.failed_codes:
            return self.failed_codes[0]
        if self.status == "cancelled" or self.status in _TRANSIENT_RUN_STATUSES:
            return "--"
        return "AC"

    @property
    def display(self) -> str:
        return "/".join(self.failed_codes) if self.failed_codes else self.short

    def match(self, expected_behavior: str) -> tuple[bool, bool, bool, str]:
        if self.status in _TRANSIENT_RUN_STATUSES:
            return (False, False, False, "running")
        if not self.completed:
            return (False, False, self.passed, "")
        matched, reason = _status_codes_rule_match(
            expected_behavior, self.failed_codes or ("AC",),
        )
        return (matched, True, self.passed, reason)


def analyze_program_result(
    run_status: str, summary: Mapping[str, object] | None,
) -> ProgramResultAnalysis:
    """Read program evidence once for expected-result matching and display."""
    if run_status == "cancelled" or run_status in _TRANSIENT_RUN_STATUSES:
        return ProgramResultAnalysis(run_status, False, False, ())
    tests = summary.get("tests") if summary is not None else None
    codes: set[str] = set()
    all_decided = True
    all_passed = isinstance(tests, list)
    if isinstance(tests, list):
        for item in tests:
            code = (
                run_verdict_short(str(item.get("verdict") or ""))
                if isinstance(item, dict) else "FL"
            )
            all_decided = all_decided and (code in _CANONICAL_DECISION_CODES or code == "SK")
            all_passed = all_passed and code in {"AC", "SK"}
            if code not in {"", "--", "AC", "SK"}:
                codes.add(code)
    completed = bool(isinstance(tests, list) and tests and all_decided)
    if summary is not None and summary.get("error") in _COMPILE_ERROR_VALUES:
        completed = True
        codes = {"CE"}
    elif summary is not None:
        tests_total = summary.get("tests_total")
        if isinstance(tests_total, int) and not isinstance(tests_total, bool):
            tests_skipped = summary.get("tests_skipped", 0)
            completed = (
                completed
                and isinstance(tests_skipped, int)
                and not isinstance(tests_skipped, bool)
                and isinstance(tests, list)
                and len(tests) + tests_skipped == tests_total
            )
    if not codes and not (isinstance(tests, list) and tests) and run_status != "ok":
        codes.add("FL")
    priority = {"CE": 0, "TL": 1, "RE": 2, "WA": 3, "FL": 4}
    failed_codes = tuple(sorted(codes, key=lambda code: (priority.get(code, 99), code)))
    return ProgramResultAnalysis(run_status, completed, completed and all_passed, failed_codes)


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


def _status_codes_rule_match(
    expected_behavior: str,
    observed_codes: list[str] | tuple[str, ...],
) -> tuple[bool, str]:
    required_codes, allowed_codes = expected_status_rule(expected_behavior)
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


def _status_codes_allowed_match(
    expected_behavior: str,
    observed_code: str,
) -> tuple[bool, str]:
    _required_codes, allowed_codes = expected_status_rule(expected_behavior)
    if observed_code in allowed_codes:
        return (True, "")
    return (
        False,
        f"allowed={_status_codes_display(allowed_codes)}, "
        f"got={_status_codes_display([observed_code])}",
    )


def verification_verdict_match(
    expected_behavior: str,
    verdict: str,
) -> tuple[bool, bool, bool, str]:
    """Match one terminal verdict against the expected behavior's allowed set."""

    observed_code = run_verdict_short(verdict.upper())
    if observed_code not in _CANONICAL_DECISION_CODES:
        return (False, False, False, "")
    observed_pass = observed_code == "AC"
    matched, reason = _status_codes_allowed_match(
        expected_behavior,
        observed_code,
    )
    if matched:
        return (True, True, observed_pass, "")
    return (False, True, observed_pass, reason or "verification mismatch")


def verification_case_result_match(
    expected_behavior: str,
    result: ExecutionResult,
) -> tuple[bool, bool, bool, str]:
    """Validate one testcase decision without applying program-wide requirements.

    Transport and batch status do not define whether a judging decision is
    complete. CE, RE, TL, WA, and AC are all complete decisions; FL, missing,
    and unknown verdicts are infrastructure/incomplete outcomes. Required
    verdicts are checked only after every testcase for the program is terminal.
    """

    return verification_verdict_match(
        expected_behavior,
        result.verdict,
    )


def verification_program_results_match(
    expected_behavior: str,
    results: Iterable[ExecutionResult],
) -> tuple[bool, str]:
    """Match the durable decisions for one completed verification program."""

    observed_codes: list[str] = []
    saw_result = False
    for result in results:
        saw_result = True
        observed_code = run_verdict_short(result.verdict.upper())
        if observed_code == "SK":
            continue
        if observed_code not in _CANONICAL_DECISION_CODES:
            return (False, "completed solution program has an incomplete result")
        observed_codes.append(observed_code)
    if not saw_result:
        return (False, "completed solution program has no testcase results")
    if not observed_codes:
        return (True, "")
    return _status_codes_rule_match(expected_behavior, observed_codes)
