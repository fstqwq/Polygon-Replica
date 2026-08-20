import re
from pathlib import Path

from app.impl.auth.shared import parse_iso_utc
from app.impl.runtime.dependency import runtime
from app.service.access.model import VerificationAccessContext
from app.service.repository.revision import verification_source_display
from app.service.platform.error_text import bounded_display_text, normalize_display_text
from app.service.problem.solution_metadata import normalize_expected_behavior
from app.service.verification.result_match import (
    run_verdict_short,
    verification_verdict_match,
)

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


def _run_expected_behavior_from_summary(summary: dict | None) -> str:
    if summary is not None:
        raw = summary.get("expected_behavior")
        if isinstance(raw, str):
            return normalize_expected_behavior(raw)
    return "unknown"


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
    matched, completed, _observed_pass, _reason = verification_verdict_match(
        normalized,
        short,
    )
    if not completed or not matched:
        return "fail"
    return "neutral" if short == "AC" else "expected-nonac"


def _run_task_kind_from_summary(summary: dict | None) -> str:
    if summary is None:
        return ""
    task_kind = summary.get("task_kind")
    return task_kind if isinstance(task_kind, str) else ""


def _is_main_correct_task_kind(task_kind: str) -> bool:
    return task_kind == _TASK_KIND_MAIN_CORRECT


def _normalized_verification_status(status: str) -> str:
    token = status
    if token in {"queued", "pending", "running"}:
        return "running"
    if token in {"failed", "cancelled"}:
        return token
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


def _verification_row_to_list_item(
    row: dict[str, object],
    *,
    access: VerificationAccessContext,
) -> dict[str, object] | None:
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
    if not access["can_view"]:
        return None
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
        "is_cancelled": status == "cancelled",
        "can_rejudge": access["can_rejudge"],
        "can_cancel": access["can_cancel"],
    }


def run_list_rows(problem_id: int, workspace_id: int, workspace: Path, limit: int = 40, actor_user_id: int | None = None) -> list[dict]:
    if actor_user_id is None:
        raise ValueError("actor_user_id is required")
    limit_cap = max(1, int(limit))
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    revision_cache: dict[str, int | None] = {}
    verification_rows = runtime().verification_service.list_visible_verification_rows(
        int(problem_id),
        int(workspace_id),
        limit=max(limit_cap * 2, 80),
    )
    problem_access = runtime().access_query.problem_context(problem_id, actor_user_id)
    access_contexts = runtime().access_query.verification_contexts(
        actor_user_id=actor_user_id,
        actor_workspace_id=workspace_id,
        expected_problem_id=problem_id,
        verifications=verification_rows,
        problem_access=problem_access,
    )
    for row, access in zip(verification_rows, access_contexts, strict=True):
        item = _verification_row_to_list_item(
            row,
            access=access,
        )
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
