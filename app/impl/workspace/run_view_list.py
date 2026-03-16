from __future__ import annotations
import re
from pathlib import Path
from app.impl.auth.shared import parse_iso_utc
from app.impl.runtime.config import config
from app.service.verification.runtime import effective_run_timeout_ms
from .problem_config import coerce_int, normalize_problem_mode
from .run_display import (
    run_verdict_short,
)
from .context_operation import (
    parse_summary_json,
)
from .context_verification import (
    _expected_status_rule,
    _verification_solution_match,
)
from .context_run_detail import (
    normalize_run_id_token,
    _verification_status_summary,
    _run_rejudge_context_for_entries,
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
from app.service.verification.store import (
    list_verification_rows,
    verification_run,
    verification_run_ids,
    verification_source_paths,
)

_C = config.constants


def _latest_iso_timestamp(values: list[str] | tuple[str, ...]) -> str:
    parsed_values = []
    for raw in values:
        token = raw
        parsed = parse_iso_utc(token)
        if parsed is None:
            continue
        parsed_values.append((parsed, token))
    if not parsed_values:
        return ""
    return max(parsed_values, key=lambda item: item[0])[1]


def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if summary is None:
        return 0
    limits = summary.get("limits")
    if limits is not None:
        wall_ms = coerce_int(limits.get("wall_ms"), 0, 0, 10**9)
        if wall_ms > 0:
            return wall_ms
        cpu_ms = coerce_int(limits.get("cpu_ms"), 0, 0, 10**9)
        if cpu_ms > 0:
            return cpu_ms
    run_cfg = summary.get("run_config")
    if run_cfg is None:
        return 0
    mode = normalize_problem_mode(run_cfg.get("mode"), str(_C.GENERAL_CONFIG_DEFAULTS["mode"]))
    time_limit_ms = coerce_int(run_cfg.get("time_limit_ms"), 0, 0, int(_C.GENERAL_TIME_LIMIT_MAX_MS))
    if time_limit_ms > 0:
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
    run_timeout_ms = coerce_int(run_cfg.get("run_timeout_ms"), 0, 0, 10**9)
    if run_timeout_ms > 0:
        return run_timeout_ms
    run_timeout_sec = coerce_int(run_cfg.get("run_timeout_sec"), 0, 0, 10**6)
    if run_timeout_sec > 0:
        return run_timeout_sec * 1000
    return 0


def _run_test_sort_key(test_name: str) -> tuple[int, str]:
    token = Path(test_name).name
    stem = token.rsplit(".", 1)[0]
    if stem.isdigit():
        return (int(stem), token)
    match = re.search(r"(\d+)", stem)
    if match is not None:
        return (int(match.group(1)), token)
    return (10**9, token)


def _run_test_answer_name(test_name: str) -> str:
    token = Path(test_name).name
    if token.lower().endswith(".in"):
        return token[:-3] + ".ans"
    return token + ".ans" if token else ""


def _run_expected_behavior_from_summary(summary: dict | None, source: str = "") -> str:
    if summary is not None:
        token = normalize_expected_behavior(summary.get("expected_behavior"))
        if token != "unknown":
            return token
        summary_source = summary.get("source")
        if summary_source is not None:
            source = summary_source
    return normalize_expected_behavior(infer_expected_behavior_from_name(source))


def _run_cell_kind(verdict: str, expected_behavior: str) -> str:
    short = run_verdict_short(verdict)
    if short in {"", "-", "--"}:
        return "neutral"
    if short == "FL":
        return "fail"
    required_codes, allowed_codes = _expected_status_rule(expected_behavior)
    if expected_behavior == "unknown":
        return "neutral"
    allowed = set(allowed_codes)
    required = set(required_codes)
    has_required = True if not required else short in required
    is_allowed = short in allowed
    if short == "AC":
        return "ok" if (has_required and is_allowed) else "neutral"
    return "expected-nonac" if (has_required and is_allowed) else "fail"

def _verification_source_from_summary(summary: dict | None) -> str:
    if summary is None:
        return ''
    verification = summary.get("verification") or {}
    verification_source = summary.get("verification_source")
    if verification_source is not None:
        return verification_source
    verification_source = verification.get("source")
    if verification_source is not None:
        return verification_source
    return ''


def _is_main_correct_verification_source(source: str) -> bool:
    return source == 'verification.solve-main'

def _is_solution_list_source(source: str) -> bool:
    return bool(normalize_optional_component_source_path_safe(source, "solutions", "solution path"))

def _verification_runs_for_list(summary: dict[str, object], *, fallback_status: str) -> list[dict[str, object]]:
    source_paths = verification_source_paths(summary)
    source_by_run_id: dict[str, str] = {}
    expected_by_run_id: dict[str, str] = {}
    matched_by_run_id: dict[str, bool] = {}
    completed_by_run_id: dict[str, bool] = {}
    passed_by_run_id: dict[str, bool] = {}
    reason_by_run_id: dict[str, str] = {}
    solutions = summary.get("solutions")
    if solutions is not None:
        for item in solutions:
            run_id = normalize_run_id_token(item.get("run_id"))
            if not run_id:
                continue
            source_token = normalize_optional_component_source_path_safe(
                item.get("source_path"),
                "solutions",
                "solution path",
            )
            if source_token:
                source_by_run_id[run_id] = source_token
            expected_behavior = item.get("expected_behavior")
            expected_by_run_id[run_id] = normalize_expected_behavior(expected_behavior)
            matched_by_run_id[run_id] = bool(item.get("matched"))
            completed_by_run_id[run_id] = bool(item.get("completed"))
            passed_by_run_id[run_id] = bool(item.get("passed_all_tests"))
            reason_text = item.get("reason")
            reason_by_run_id[run_id] = "" if reason_text is None else reason_text
    runs: list[dict[str, object]] = []
    run_ids = verification_run_ids(summary)
    for idx, run_id in enumerate(run_ids):
        run_id = normalize_run_id_token(run_id)
        if not run_id:
            continue
        run_row = verification_run(summary, run_id)
        run_summary = dict(run_row.get("summary") or {})
        status_text = run_row.get("status") or ""
        if not status_text:
            status_text = run_summary.get("status") or ""
        if not status_text:
            status_text = fallback_status
        if not status_text:
            status_text = "running"
        source = run_summary.get("source") or run_row.get("source_label") or ""
        if not source:
            source = source_by_run_id.get(run_id, "")
        if (not source) and idx < len(source_paths):
            source = source_paths[idx]
        raw_expected_behavior = expected_by_run_id.get(run_id)
        if raw_expected_behavior == "unknown":
            raw_expected_behavior = run_row.get("expected_behavior")
        expected_behavior = normalize_expected_behavior(raw_expected_behavior)
        if expected_behavior == "unknown":
            expected_behavior = _run_expected_behavior_from_summary(run_summary, source)
        verification_source = _verification_source_from_summary(run_summary)
        if not verification_source:
            verification_source = summary.get("verification_source") or ""
        if run_id in matched_by_run_id:
            matched = bool(matched_by_run_id[run_id])
            completed = bool(completed_by_run_id.get(run_id))
            observed_pass = bool(passed_by_run_id.get(run_id))
            reason = reason_by_run_id.get(run_id, "")
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
                "reason": reason,
                "verification_source": verification_source,
                "is_main_correct_run": _is_main_correct_verification_source(verification_source),
                "summary_loaded": bool(run_summary),
                "summary": run_summary,
            }
        )
    if runs:
        return runs
    for idx, source in enumerate(source_paths, start=1):
        status_text = fallback_status or "running"
        if not status_text:
            status_text = "running"
        verification_source = summary.get("verification_source") or ""
        runs.append(
            {
                "id": f"run-{idx}",
                "source": source,
                "status": status_text,
                "tests_total": 0,
                "expected_behavior": normalize_expected_behavior(infer_expected_behavior_from_name(source)),
                "expected_behavior_label": expected_behavior_label(normalize_expected_behavior(infer_expected_behavior_from_name(source))),
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "pending",
                "verification_source": verification_source,
                "is_main_correct_run": False,
                "summary_loaded": False,
                "summary": {},
            }
        )
    if runs:
        return runs
    solution_count = coerce_int(summary.get("solution_count"), 0, 0, 10**6)
    if solution_count <= 0:
        solution_count = 1
    for idx in range(1, solution_count + 1):
        status_text = fallback_status or "running"
        if not status_text:
            status_text = "running"
        verification_source = summary.get("verification_source") or ""
        runs.append(
            {
                "id": f"run-{idx}",
                "source": "",
                "status": status_text,
                "tests_total": 0,
                "expected_behavior": "unknown",
                "expected_behavior_label": expected_behavior_label("unknown"),
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "pending",
                "verification_source": verification_source,
                "is_main_correct_run": False,
                "summary_loaded": False,
                "summary": {},
            }
        )
    return runs


def _verification_solution_entries_for_list(
    summary: dict[str, object],
    *,
    fallback_status: str,
    runs: list[dict[str, object]],
) -> list[dict[str, object]]:
    solution_sources = verification_source_paths(summary)
    actual_solution_runs = [item for item in runs if _is_solution_list_source(item["source"])]
    runs_by_source: dict[str, dict[str, object]] = {}
    for item in actual_solution_runs:
        source = item["source"]
        if source and source not in runs_by_source:
            runs_by_source[source] = item
    entries: list[dict[str, object]] = []
    for source in solution_sources:
        item = runs_by_source.get(source)
        if item is not None:
            entries.append(item)
            continue
        status_text = fallback_status or "running"
        entries.append(
            {
                "id": "",
                "source": source,
                "status": status_text,
                "tests_total": 0,
                "expected_behavior": normalize_expected_behavior(infer_expected_behavior_from_name(source)),
                "expected_behavior_label": expected_behavior_label(normalize_expected_behavior(infer_expected_behavior_from_name(source))),
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "pending",
                "verification_source": summary.get("verification_source") or "",
                "is_main_correct_run": False,
                "summary_loaded": False,
                "summary": {},
            }
        )
    for item in actual_solution_runs:
        source = item["source"]
        if source and source not in solution_sources:
            entries.append(item)
    if entries:
        return entries
    solution_count = coerce_int(summary.get("solution_count"), 0, 0, 10**6)
    if solution_count <= 0:
        solution_count = 1
    status_text = fallback_status or "running"
    entries = []
    for idx in range(1, solution_count + 1):
        entries.append(
            {
                "id": "",
                "source": "",
                "status": status_text,
                "tests_total": 0,
                "expected_behavior": "unknown",
                "expected_behavior_label": expected_behavior_label("unknown"),
                "matched": False,
                "completed": False,
                "passed_all_tests": False,
                "reason": "pending",
                "verification_source": summary.get("verification_source") or "",
                "is_main_correct_run": False,
                "summary_loaded": False,
                "summary": {},
            }
        )
    return entries


def _verification_row_to_list_item(problem_id: int, workspace: Path, row: dict[str, object]) -> dict[str, object] | None:
    verification_id = normalize_run_id_token(row.get("id"))
    if not verification_id:
        return None
    row_status = row.get("status") or ""
    status_text = row_status or "running"
    summary = parse_summary_json(row.get("summary_json"), f"verification/list/{verification_id}")
    summary_status = summary.get("status") or ""
    if summary_status and not row_status:
        status_text = summary_status
    runs = _verification_runs_for_list(summary, fallback_status=status_text)
    solution_entries = _verification_solution_entries_for_list(summary, fallback_status=status_text, runs=runs)
    if not solution_entries:
        return None
    status_summary = _verification_status_summary(solution_entries)
    if status_text in {"running", "queued", "pending"}:
        status_summary = {
            **status_summary,
            "status": "running",
            "status_upper": "RUNNING",
            "is_failed": False,
            "has_running": True,
        }
    elif status_text in {"failed", "cancelled"}:
        status_summary = {
            **status_summary,
            "status": "failed",
            "status_upper": "FAILED",
            "is_failed": True,
            "has_running": False,
        }
    elif status_text in {"ok", "pass"}:
        status_summary = {
            **status_summary,
            "status": "ok",
            "status_upper": "OK",
            "is_failed": False,
            "has_running": False,
        }
    has_running = bool(status_summary.get("has_running"))
    matched_count = coerce_int(status_summary.get("matched_count"), 0, 0, 10**9)
    total_count = coerce_int(status_summary.get("total_count"), len(solution_entries), 0, 10**9)
    completed_members = [
        item
        for item in solution_entries
        if item.get("status") not in {"running", "queued", "pending"}
    ]
    matched_completed_count = sum((1 for item in completed_members if bool(item.get("matched"))))
    tests_total = 0
    test_totals = [
        coerce_int(item.get("tests_total"), 0, 0, 10**9)
        for item in runs
        if coerce_int(item.get("tests_total"), 0, 0, 10**9) > 0
    ]
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
    source_items = [item["source"] for item in solution_entries if item["source"]]
    source_display = "-"
    if source_items:
        shown = source_items[:2]
        extra = len(source_items) - len(shown)
        source_display = ", ".join(shown)
        if extra > 0:
            source_display += f", +{extra}"
    rejudge_context = _run_rejudge_context_for_entries(solution_entries, workspace)
    rerun_paths = rejudge_context["paths"]
    run_ids: list[str] = []
    for item in solution_entries:
        token = normalize_run_id_token(item.get("id"))
        if token:
            run_ids.append(token)
    artifact_verification_id = row.get("artifact_verification_id") or ""
    mode = summary.get("mode") or ""
    if not mode:
        mode = row.get("kind") or ""
    created_at = row.get("created_at") or ""
    rerun_query = rejudge_context["query"]
    rerun_unavailable_reason = rejudge_context["unavailable_reason"]
    return {
        "index": 0,
        "id": verification_id,
        "verification_id": verification_id,
        "run_ids": run_ids,
        "run_ids_csv": ",".join(run_ids),
        "run_count": total_count,
        "artifact_verification_id": artifact_verification_id,
        "mode": mode,
        "status": status_summary["status"],
        "status_upper": status_summary["status_upper"],
        "created_at": created_at,
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
        "rerun_solution_query": rerun_query,
        "rerun_unavailable_reason": rerun_unavailable_reason,
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
        if item is None:
            continue
        token = item["id"]
        if not token or token in seen_ids:
            continue
        seen_ids.add(token)
        result.append(item)

    def _row_sort_key(item: dict[str, object]) -> tuple[int, float, str]:
        raw = item.get("created_at") or ""
        parsed = parse_iso_utc(raw)
        if parsed is None:
            return (0, -1.0, raw)
        return (1, float(parsed.timestamp()), raw)

    result.sort(key=_row_sort_key, reverse=True)
    trimmed = result[:limit_cap]
    for idx, row in enumerate(trimmed, start=1):
        row["index"] = idx
    return trimmed
