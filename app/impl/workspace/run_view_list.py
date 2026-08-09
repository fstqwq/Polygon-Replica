from __future__ import annotations

import re
from pathlib import Path

from app.impl.auth.shared import parse_iso_utc
from app.impl.runtime.config import config
from app.service.repository.revision import verification_source_display
from app.service.platform.error_text import bounded_display_text, normalize_display_text
from app.service.problem.solution_metadata import infer_expected_behavior_from_name, normalize_expected_behavior
from app.service.verification.runtime import coerce_int, normalize_problem_mode

from app.service.verification.result_match import run_verdict_short, verification_solution_match

_C = config.constants
_TASK_KIND_MAIN_CORRECT = "main-correct"
_TEST_NAME_NUM_RE = re.compile(r"^(\d+)\.in$")
_RUN_LIST_REASON_LIMIT_BYTES = 180
_SANITY_STATUS_TOKENS = {"ok", "passed", "pending", "running", "warning", "failed", "skipped"}


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


def _run_test_sort_key(test_name: str) -> tuple[int, str]:
    token = Path(test_name).name
    match = _TEST_NAME_NUM_RE.fullmatch(token)
    if match is not None:
        return (int(match.group(1)), token)
    stem = token.rsplit(".", 1)[0]
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
        if isinstance(summary_source, str) and summary_source:
            source = summary_source
    return normalize_expected_behavior(infer_expected_behavior_from_name(source))


def _run_cell_kind(verdict: str, expected_behavior: str) -> str:
    if verdict in {"", "-", "--", ".."}:
        return "neutral"
    short = run_verdict_short(verdict)
    if short in {"", "-", "--"}:
        return "neutral"
    if short == "FL":
        return "fail"
    normalized = normalize_expected_behavior(expected_behavior)
    if normalized == "accepted":
        return "ok" if short == "AC" else "fail"
    if normalized == "unknown":
        return "neutral"
    matched, _completed, _observed_pass, _reason = verification_solution_match(
        normalized,
        "ok",
        {"tests": [{"verdict": short}]},
    )
    return "expected-nonac" if matched else "neutral"


def _run_task_kind_from_summary(summary: dict | None) -> str:
    if summary is None:
        return ""
    task_kind = summary.get("task_kind")
    return task_kind if isinstance(task_kind, str) else ""


def _is_main_correct_task_kind(task_kind: str) -> bool:
    return task_kind == _TASK_KIND_MAIN_CORRECT


def _run_timeout_ms_from_summary(summary: dict | None) -> int:
    if summary is None:
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
    if time_limit_ms <= 0:
        return 0
    if mode == "interactive":
        pass_limit = coerce_int(run_cfg.get("pass_limit"), 1, 1, 100)
        if pass_limit > 1:
            return time_limit_ms + int(_C.RUN_WALL_TIME_SLACK_PASS_LIMIT_SEC) * 1000
        return time_limit_ms + int(_C.RUN_WALL_TIME_SLACK_INTERACTIVE_SEC) * 1000
    return time_limit_ms + int(_C.RUN_WALL_TIME_SLACK_PASS_FAIL_SEC) * 1000


def _normalized_verification_status(status: str) -> str:
    token = status
    if token in {"queued", "pending", "running"}:
        return "running"
    if token == "failed":
        return "failed"
    if token == "ok":
        return "ok"
    return token


def _list_reason_display(raw: object) -> tuple[str, str]:
    full_text = normalize_display_text(str(raw or ""))
    if not full_text:
        return ("", "")
    compact_text = " ".join(part.strip() for part in full_text.splitlines() if part.strip())
    if not compact_text:
        compact_text = full_text
    display_text = bounded_display_text(compact_text, limit_bytes=_RUN_LIST_REASON_LIMIT_BYTES)
    title_text = full_text if (full_text != compact_text or full_text != display_text) else ""
    return (display_text, title_text)


def _verification_row_to_list_item(row: dict[str, object]) -> dict[str, object] | None:
    verification_id = str(row.get("id") or "")
    if not verification_id:
        return None
    status = _normalized_verification_status(str(row.get("status") or ""))
    sanity_status = str(row.get("sanity_status") or "").strip().lower()
    if sanity_status not in _SANITY_STATUS_TOKENS:
        sanity_status = "unknown"
    sanity_attention = sanity_status in {"warning", "failed"}
    reason_source = row.get("fail_reason") or ""
    if status == "ok" and sanity_attention:
        reason_source = row.get("error") or ""
    fail_reason_display, fail_reason_title = _list_reason_display(reason_source)
    status_display = status
    if status == "ok" and sanity_status == "warning":
        status_display = "ok (has warning)"
    elif status == "ok" and sanity_status == "failed":
        status_display = "ok (sanity failed)"
    return {
        "index": 0,
        "id": verification_id,
        "verification_id": verification_id,
        "kind": str(row.get("kind") or ""),
        "created_at": str(row.get("created_at") or ""),
        "finished_at": str(row.get("finished_at") or ""),
        "status": status,
        "status_display": status_display,
        "status_tone": "warn" if status == "ok" and sanity_attention else status,
        "sanity_status": sanity_status,
        "fail_reason": str(reason_source or ""),
        "fail_reason_display": fail_reason_display,
        "fail_reason_title": fail_reason_title,
        "has_running": status == "running",
        "is_failed": status == "failed",
    }


def run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int = 40, actor_user_id: int | None = None) -> list[dict]:
    _ = actor_user_id
    limit_cap = max(1, int(limit))
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    revision_cache: dict[str, int | None] = {}
    verification_rows = config.verification_service.list_workspace_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=max(limit_cap * 2, 80),
        kinds=("all", "sample", "custom"),
    )
    for row in verification_rows:
        item = _verification_row_to_list_item(row)
        if item is None:
            continue
        item["source_display"] = verification_source_display(
            workspace,
            str(row.get("source_commit") or ""),
            revision_cache,
        )
        token = str(item["id"])
        if (not token) or token in seen_ids:
            continue
        seen_ids.add(token)
        result.append(item)

    def _row_sort_key(item: dict[str, object]) -> tuple[int, float, str]:
        raw = str(item.get("created_at") or "")
        parsed = parse_iso_utc(raw)
        if parsed is None:
            return (0, -1.0, raw)
        return (1, float(parsed.timestamp()), raw)

    result.sort(key=_row_sort_key, reverse=True)
    trimmed = result[:limit_cap]
    for idx, row in enumerate(trimmed, start=1):
        row["index"] = idx
    return trimmed
