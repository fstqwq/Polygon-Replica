from collections.abc import Mapping
from app.impl.workspace.context_operation import dedupe_preserve_order
from app.impl.workspace.context_run_detail import normalize_run_test_name_token


def _verification_tests_meta_stats(
    summary: Mapping[str, object] | None,
) -> dict[str, int]:
    if summary is None:
        return {"total": 0}
    tests_total = _summary_int(summary.get("tests_total"), field="tests_total")
    if tests_total > 0:
        return {"total": tests_total}
    selected_count = _summary_int(
        summary.get("selected_tests_count"),
        field="selected_tests_count",
    )
    if selected_count > 0:
        return {"total": selected_count}
    selected_tests_raw = summary.get("selected_tests")
    if selected_tests_raw is not None:
        if not isinstance(selected_tests_raw, list):
            raise RuntimeError("verification summary selected_tests must be a list")
        selected_test_names: list[str] = []
        for item in selected_tests_raw:
            if not isinstance(item, str):
                raise RuntimeError("verification summary selected_tests must contain text")
            selected_test_names.append(item)
        selected_tests = dedupe_preserve_order(
            [normalize_run_test_name_token(item) for item in selected_test_names]
        )
        if selected_tests:
            return {"total": len(selected_tests)}
    return {"total": _run_test_count_from_summary(summary)}


def _summary_int(value: object, *, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"verification summary {field} must be an integer")
    return value


def _run_test_count_from_summary(summary: Mapping[str, object] | None) -> int:
    if summary is None:
        return 0
    if bool(summary.get('execution_skipped')):
        return 0
    tests = summary.get('tests')
    if tests is not None:
        if not isinstance(tests, list):
            raise RuntimeError("verification summary tests must be a list")
        return len(tests)
    usage = summary.get('usage')
    if usage is not None:
        if not isinstance(usage, dict):
            raise RuntimeError("verification summary usage must be an object")
        test_count = _summary_int(usage.get("tests"), field="usage.tests")
        return max(0, test_count)
    return 0
