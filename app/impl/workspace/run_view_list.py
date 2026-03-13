from __future__ import annotations
import re
from pathlib import Path
from app.impl.auth.public import parse_iso_utc
from app.impl.runtime.config import config
from app.service.build.runtime import effective_run_timeout_ms, wall_time_slack_sec_for_mode
from .problem_config import coerce_int, normalize_problem_mode
from .run_display import (
    run_actual_display,
    run_actual_short,
    run_cpu_wall_ms_text,
    run_error_display,
    run_memory_mb_text,
    run_verdict_short,
)
from .context_operation import (
    dedupe_preserve_order,
    _expected_status_rule,
    parse_summary_json,
    _verification_solution_match,
)
from .context_run_detail import (
    normalize_run_id_token,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
    _run_source_from_summary,
)
from app.main_util import (
    normalize_optional_component_source_path_safe,
)
from app.service.problem.solution_metadata import (
    expected_behavior_label,
    infer_expected_behavior_from_name,
    normalize_expected_behavior,
)
from .run_view_lifecycle_card import _run_test_count_from_summary
from app.service.verification import (
    list_verification_rows,
    load_verification_summary,
    verification_run,
    verification_run_ids,
    verification_source_paths,
)

_C = config.constants
_run_actual_display = run_actual_display
_run_actual_short = run_actual_short
_run_cpu_wall_ms_text = run_cpu_wall_ms_text
_run_error_display = run_error_display
_run_memory_mb_text = run_memory_mb_text
_run_verdict_short = run_verdict_short


def _earliest_iso_timestamp(values: list[str] | tuple[str, ...]) -> str:
    parsed_values = []
    for raw in values:
        token = str(raw or "").strip()
        parsed = parse_iso_utc(token)
        if parsed is None:
            continue
        parsed_values.append((parsed, token))
    if not parsed_values:
        return ""
    return min(parsed_values, key=lambda item: item[0])[1]


def _latest_iso_timestamp(values: list[str] | tuple[str, ...]) -> str:
    parsed_values = []
    for raw in values:
        token = str(raw or "").strip()
        parsed = parse_iso_utc(token)
        if parsed is None:
            continue
        parsed_values.append((parsed, token))
    if not parsed_values:
        return ""
    return max(parsed_values, key=lambda item: item[0])[1]


def _wall_time_slack_sec_for_mode(mode: object) -> int:
    return wall_time_slack_sec_for_mode(
        mode,
        pass_fail_sec=int(_C.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC),
        multi_pass_sec=int(_C.RUN_WALL_TIME_SLACK_MULTI_PASS_SEC),
        interactive_sec=int(_C.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC),
    )


def _effective_run_timeout_ms(time_limit_ms: int, *, mode: object = "pass-fail") -> int:
    return effective_run_timeout_ms(
        time_limit_ms,
        mode=mode,
        default_ms=int(_C.GENERAL_CONFIG_DEFAULTS["time_limit_ms"]),
        min_ms=int(_C.GENERAL_TIME_LIMIT_MIN_MS),
        max_ms=int(_C.GENERAL_TIME_LIMIT_MAX_MS),
        pass_fail_slack_sec=int(_C.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC),
        multi_pass_slack_sec=int(_C.RUN_WALL_TIME_SLACK_MULTI_PASS_SEC),
        interactive_slack_sec=int(_C.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC),
    )


def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    limits = summary.get("limits")
    if isinstance(limits, dict):
        wall_ms = coerce_int(limits.get("wall_ms"), 0, 0, 10**9)
        if wall_ms > 0:
            return wall_ms
        cpu_ms = coerce_int(limits.get("cpu_ms"), 0, 0, 10**9)
        if cpu_ms > 0:
            return cpu_ms
    run_cfg = summary.get("run_config")
    if not isinstance(run_cfg, dict):
        return 0
    mode = normalize_problem_mode(run_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
    time_limit_ms = coerce_int(run_cfg.get("time_limit_ms"), 0, 0, int(_C.GENERAL_TIME_LIMIT_MAX_MS))
    if time_limit_ms > 0:
        return _effective_run_timeout_ms(time_limit_ms, mode=mode)
    run_timeout_ms = coerce_int(run_cfg.get("run_timeout_ms"), 0, 0, 10**9)
    if run_timeout_ms > 0:
        return run_timeout_ms
    run_timeout_sec = coerce_int(run_cfg.get("run_timeout_sec"), 0, 0, 10**6)
    if run_timeout_sec > 0:
        return run_timeout_sec * 1000
    return 0


def _run_test_sort_key(test_name: str) -> tuple[int, str]:
    token = Path(str(test_name or "").strip()).name
    stem = token.rsplit(".", 1)[0]
    if stem.isdigit():
        return (int(stem), token)
    match = re.search(r"(\d+)", stem)
    if match is not None:
        return (int(match.group(1)), token)
    return (10**9, token)


def _run_test_answer_name(test_name: str) -> str:
    token = Path(str(test_name or "").strip()).name
    if token.lower().endswith(".in"):
        return token[:-3] + ".ans"
    return token + ".ans" if token else ""


def _run_expected_behavior_from_summary(summary: dict | None, source: str = "") -> str:
    if isinstance(summary, dict):
        token = normalize_expected_behavior(summary.get("expected_behavior"))
        if token != "unknown":
            return token
        source = str(summary.get("source") or source or "").strip()
    else:
        source = str(source or "").strip()
    return normalize_expected_behavior(infer_expected_behavior_from_name(source))


def _run_cell_kind(verdict: str, expected_behavior: str) -> str:
    short = _run_verdict_short(verdict)
    if short in {"", "-", "--"}:
        return "neutral"
    if short in {"FL", "CE"}:
        return "fail"
    _normalized, required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    allowed = set(allowed_codes)
    required = set(required_codes)
    if short not in allowed:
        return "fail"
    if not required:
        return "ok" if short == "AC" else "neutral"
    if short in required:
        return "ok" if short == "AC" else "expected-nonac"
    return "neutral"

def _verification_source_from_summary(summary: dict | None) -> str:
    if not isinstance(summary, dict):
        return ''
    return str(summary.get('verification_source') or '').strip().lower()


def _is_main_correct_verification_source(source: object) -> bool:
    return str(source or '').strip().lower() == 'build.solve'

def _verification_runs_for_list(summary: dict[str, object], *, fallback_status: str) -> list[dict[str, object]]:
    source_paths = verification_source_paths(summary)
    source_by_run_id: dict[str, str] = {}
    expected_by_run_id: dict[str, str] = {}
    matched_by_run_id: dict[str, bool] = {}
    completed_by_run_id: dict[str, bool] = {}
    passed_by_run_id: dict[str, bool] = {}
    reason_by_run_id: dict[str, str] = {}
    solutions_obj = summary.get("solutions")
    if isinstance(solutions_obj, list):
        for item in solutions_obj:
            if not isinstance(item, dict):
                continue
            run_id = normalize_run_id_token(item.get("run_id"))
            if not run_id:
                continue
            source_token = normalize_optional_component_source_path_safe(
                str(item.get("source_path") or ""),
                "solutions",
                "solution path",
            )
            if source_token:
                source_by_run_id[run_id] = source_token
            expected_by_run_id[run_id] = normalize_expected_behavior(str(item.get("expected_behavior") or "unknown"))
            matched_by_run_id[run_id] = bool(item.get("matched"))
            completed_by_run_id[run_id] = bool(item.get("completed"))
            passed_by_run_id[run_id] = bool(item.get("passed_all_tests"))
            reason_by_run_id[run_id] = str(item.get("reason") or "")
    runs: list[dict[str, object]] = []
    run_ids = verification_run_ids(summary)
    for idx, run_id in enumerate(run_ids):
        run_id = normalize_run_id_token(run_id)
        if not run_id:
            continue
        run_obj = verification_run(summary, run_id)
        run_summary = run_obj.get("summary") if isinstance(run_obj, dict) else None
        run_summary = dict(run_summary) if isinstance(run_summary, dict) else {}
        status_text = str(run_obj.get("status") or run_summary.get("status") or fallback_status or "running").strip().lower() or "running"
        source = str(run_summary.get("source") or run_obj.get("source_label") or "").strip()
        if not source:
            source = str(source_by_run_id.get(run_id) or "").strip()
        if (not source) and idx < len(source_paths):
            source = str(source_paths[idx] or "").strip()
        expected_behavior = normalize_expected_behavior(
            str(expected_by_run_id.get(run_id) or run_obj.get("expected_behavior") or "")
        )
        if expected_behavior == "unknown":
            expected_behavior = _run_expected_behavior_from_summary(run_summary, source)
        verification_source = _verification_source_from_summary(run_summary) or str(summary.get("verification_source") or "").strip().lower()
        if run_id in matched_by_run_id:
            matched = bool(matched_by_run_id[run_id])
            completed = bool(completed_by_run_id.get(run_id))
            observed_pass = bool(passed_by_run_id.get(run_id))
            reason = str(reason_by_run_id.get(run_id) or "")
        else:
            matched, completed, observed_pass, reason = _verification_solution_match(expected_behavior, status_text, run_summary)
        runs.append(
            {
                "id": run_id,
                "source": source,
                "status": status_text,
                "tests_total": _run_test_count_from_summary(run_summary),
                "expected_behavior": expected_behavior,
                "expected_behavior_label": expected_behavior_label(expected_behavior),
                "matched": bool(matched),
                "completed": bool(completed),
                "passed_all_tests": bool(observed_pass),
                "reason": str(reason or ""),
                "verification_source": verification_source,
                "is_main_correct_run": _is_main_correct_verification_source(verification_source),
                "summary_loaded": bool(run_summary),
                "summary": run_summary,
            }
        )
    if runs:
        return runs
    for idx, source in enumerate(source_paths, start=1):
        runs.append(
            {
                "id": f"run-{idx}",
                "source": source,
                "status": str(fallback_status or "running").strip().lower() or "running",
                "tests_total": 0,
                "expected_behavior": normalize_expected_behavior(infer_expected_behavior_from_name(source)),
                "expected_behavior_label": expected_behavior_label(normalize_expected_behavior(infer_expected_behavior_from_name(source))),
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "pending",
                "verification_source": str(summary.get("verification_source") or "").strip().lower(),
                "is_main_correct_run": False,
                "summary_loaded": False,
                "summary": {},
            }
        )
    return runs


def _verification_row_to_list_item(problem_id: int, workspace: Path, row: dict[str, object]) -> dict[str, object] | None:
    verification_id = normalize_run_id_token(row.get("id"))
    if not verification_id:
        return None
    row_status = str(row.get("status") or "").strip().lower()
    status_text = row_status or "running"
    summary = parse_summary_json(row.get("summary_json"), f"verification/list/{verification_id}")
    summary_status = str(summary.get("status") or "").strip().lower()
    if summary_status and not row_status:
        status_text = summary_status
    verification_source = str(summary.get("verification_source") or "").strip().lower()
    if verification_source in {"build.generate-input", "build.solve"}:
        return None
    runs = _verification_runs_for_list(summary, fallback_status=status_text)
    if not runs:
        return None
    status_summary = _verification_status_summary(runs)
    if status_text in {"running", "queued", "pending"}:
        status_summary = {
            **status_summary,
            "status": "running",
            "status_upper": "RUNNING",
            "is_failed": False,
            "has_running": True,
        }
    elif status_text in {"failed", "cancelled"} and not bool(status_summary.get("has_running")):
        status_summary = {
            **status_summary,
            "status": "failed",
            "status_upper": "FAILED",
            "is_failed": True,
        }
    has_running = bool(status_summary.get("has_running"))
    matched_count = int(status_summary.get("matched_count") or 0)
    total_count = int(status_summary.get("total_count") or len(runs))
    completed_members = [
        item
        for item in runs
        if str(item.get("status") or "").strip().lower() not in {"running", "queued", "pending"}
    ]
    matched_completed_count = sum((1 for item in completed_members if bool(item.get("matched"))))
    tests_total = 0
    test_totals = [int(item.get("tests_total") or 0) for item in runs if int(item.get("tests_total") or 0) > 0]
    tests_label = "tests: -"
    if has_running:
        if test_totals:
            tests_total = max(test_totals)
            tests_label = f"tests: up to {tests_total} (in progress)"
        else:
            tests_label = "tests: in progress"
    elif test_totals:
        tests_total = max(test_totals)
        min_total = min(test_totals)
        if min_total == tests_total:
            tests_label = f"tests: 1-{tests_total} (all)"
        else:
            tests_label = f"tests: {min_total}-{tests_total} (varied)"
    source_items = [str(item.get("source") or "").strip() for item in runs if str(item.get("source") or "").strip()]
    source_display = "-"
    if source_items:
        shown = source_items[:2]
        extra = len(source_items) - len(shown)
        source_display = ", ".join(shown)
        if extra > 0:
            source_display += f", +{extra}"
    rejudge_context = _run_rejudge_context_for_entries(runs, workspace)
    rerun_paths = rejudge_context.get("paths")
    if not isinstance(rerun_paths, list):
        rerun_paths = []
    return {
        "index": 0,
        "id": verification_id,
        "verification_id": verification_id,
        "run_ids": [str(item.get("id") or "") for item in runs if str(item.get("id") or "").strip()],
        "run_ids_csv": ",".join([str(item.get("id") or "") for item in runs if str(item.get("id") or "").strip()]),
        "run_count": total_count,
        "build_id": str(row.get("build_id") or "").strip(),
        "mode": str(summary.get("mode") or row.get("kind") or "").strip(),
        "status": str(status_summary.get("status") or status_text),
        "status_upper": str(status_summary.get("status_upper") or status_text.upper()),
        "created_at": str(row.get("created_at") or "").strip(),
        "source_display": source_display,
        "tests_label": tests_label,
        "tests_total": tests_total,
        "matched_count": matched_count,
        "matched_completed_count": matched_completed_count,
        "completed_count": len(completed_members),
        "has_running": has_running,
        "is_failed": bool(status_summary.get("is_failed")),
        "is_main_correct_run": False,
        "rerun_solution_paths": rerun_paths,
        "rerun_solution_query": str(rejudge_context.get("query") or ""),
        "rerun_unavailable_reason": str(rejudge_context.get("unavailable_reason") or ""),
    }


def run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int=40, actor_user_id: int | None=None) -> list[dict]:
    limit_cap = max(1, int(limit))
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    verification_rows = list_verification_rows(
        config.db,
        problem_id=int(problem_id),
        workspace_id=int(workspace_id),
        limit=max(limit_cap * 4, 80),
    )
    for row in verification_rows:
        item = _verification_row_to_list_item(int(problem_id), workspace, row)
        if not isinstance(item, dict):
            continue
        token = str(item.get("id") or "").strip()
        if not token or token in seen_ids:
            continue
        seen_ids.add(token)
        result.append(item)

    def _row_sort_key(item: dict[str, object]) -> tuple[int, float, str]:
        raw = str(item.get("created_at") or "").strip()
        parsed = parse_iso_utc(raw)
        if parsed is None:
            return (0, -1.0, raw)
        return (1, float(parsed.timestamp()), raw)

    result.sort(key=_row_sort_key, reverse=True)
    trimmed = result[:limit_cap]
    for idx, row in enumerate(trimmed, start=1):
        row["index"] = idx
    return trimmed
