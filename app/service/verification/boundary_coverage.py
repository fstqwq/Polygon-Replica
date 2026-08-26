import re
from dataclasses import dataclass

from app.service.verification.plan import VerificationTestPlan

BOUNDARY_COVERAGE_CHECK = "boundary_coverage"
MISSING_VALIDATOR_MESSAGE = (
    "No validator is configured; generated inputs use the accept-all fallback, "
    "so boundary-hit coverage cannot be checked."
)

_HIT_RE = re.compile(r'^"(?P<name>[^"]+)":(?P<hits>.*)$')
_BOUNDS_RE = re.compile(r'^constant-bounds "(?P<name>[^"]+)":\s+(?P<lower>\S+)\s+(?P<upper>\S+)\s*$')
_VARIABLE_RE = re.compile(r'^variable "(?P<name>[^"]+)"\s*$')


@dataclass
class BoundaryCoverageResult:
    status: str
    checked_count: int
    error: str
    missing: list[str]
    messages: list[str]


@dataclass
class _VariableCoverage:
    name: str
    variable_seen: bool = False
    lower_bound: str = ""
    upper_bound: str = ""
    min_hit: bool = False
    max_hit: bool = False


def boundary_coverage_missing_message(missing_item: str) -> str:
    return f"Test data did not hit: {missing_item}"


def _prepared_variable_name(raw_name: str) -> tuple[str, bool, bool]:
    name = str(raw_name or "")
    if len(name) >= 2 and name != "~~":
        ignore_min = name[0] == "~"
        ignore_max = name[-1] == "~"
        if ignore_min and ignore_max:
            return (name[1:-1], True, True)
        if ignore_min:
            return (name[1:], True, False)
        if ignore_max:
            return (name[:-1], False, True)
    return (name, False, False)


def _merge_constant_bound(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left == right:
        return left
    return "?"


def _state_for(states: dict[str, _VariableCoverage], raw_name: str) -> tuple[_VariableCoverage, bool, bool]:
    name, ignore_min, ignore_max = _prepared_variable_name(raw_name)
    state = states.get(name)
    if state is None:
        state = _VariableCoverage(name=name)
        states[name] = state
    return (state, ignore_min, ignore_max)


def extract_testlib_overview_logs(feedback_text: str) -> list[str]:
    text = str(feedback_text or "").strip()
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    if all(
        _VARIABLE_RE.fullmatch(line) is not None
        or _BOUNDS_RE.fullmatch(line) is not None
        or _HIT_RE.fullmatch(line) is not None
        for line in lines
    ):
        return ["\n".join(lines)]
    return []


def _apply_overview_log(states: dict[str, _VariableCoverage], overview_text: str) -> None:
    for raw_line in str(overview_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        variable_match = _VARIABLE_RE.fullmatch(line)
        if variable_match is not None:
            state, ignore_min, ignore_max = _state_for(states, variable_match.group("name"))
            state.variable_seen = True
            state.min_hit = state.min_hit or ignore_min
            state.max_hit = state.max_hit or ignore_max
            continue
        bounds_match = _BOUNDS_RE.fullmatch(line)
        if bounds_match is not None:
            state, ignore_min, ignore_max = _state_for(states, bounds_match.group("name"))
            state.lower_bound = _merge_constant_bound(state.lower_bound, bounds_match.group("lower"))
            state.upper_bound = _merge_constant_bound(state.upper_bound, bounds_match.group("upper"))
            state.min_hit = state.min_hit or ignore_min
            state.max_hit = state.max_hit or ignore_max
            continue
        hit_match = _HIT_RE.fullmatch(line)
        if hit_match is not None:
            state, ignore_min, ignore_max = _state_for(states, hit_match.group("name"))
            hit_tokens = set(hit_match.group("hits").split())
            state.min_hit = state.min_hit or ignore_min or "min-value-hit" in hit_tokens
            state.max_hit = state.max_hit or ignore_max or "max-value-hit" in hit_tokens


def boundary_coverage_from_feedback(
    *,
    feedback_by_test: dict[str, str],
    test_plans: list[VerificationTestPlan],
    validator_configured: bool = True,
) -> BoundaryCoverageResult:
    if not validator_configured:
        return BoundaryCoverageResult(
            status="warning",
            checked_count=0,
            error=MISSING_VALIDATOR_MESSAGE,
            missing=[],
            messages=[MISSING_VALIDATOR_MESSAGE],
        )
    selected_names = {plan.test_name for plan in test_plans if plan.test_name}
    states: dict[str, _VariableCoverage] = {}
    for test_name, feedback_text in feedback_by_test.items():
        if selected_names and test_name not in selected_names:
            continue
        for overview_text in extract_testlib_overview_logs(feedback_text):
            _apply_overview_log(states, overview_text)
    missing: list[str] = []
    checked_count = 0
    for state in sorted(states.values(), key=lambda item: item.name):
        if not (state.variable_seen or state.lower_bound or state.upper_bound):
            continue
        checked = False
        if state.lower_bound and state.lower_bound != "?":
            checked = True
            if not state.min_hit:
                missing.append(f"{state.name} min={state.lower_bound}")
        if state.upper_bound and state.upper_bound != "?":
            checked = True
            if not state.max_hit:
                missing.append(f"{state.name} max={state.upper_bound}")
        if checked:
            checked_count += 1
    if missing:
        shown = ", ".join(missing[:8])
        remaining = len(missing) - 8
        suffix = f", +{remaining} more" if remaining > 0 else ""
        return BoundaryCoverageResult(
            status="warning",
            checked_count=checked_count,
            error=boundary_coverage_missing_message(f"{shown}{suffix}"),
            missing=missing,
            messages=[boundary_coverage_missing_message(item) for item in missing],
        )
    return BoundaryCoverageResult(
        status="passed",
        checked_count=checked_count,
        error="",
        missing=[],
        messages=[],
    )
