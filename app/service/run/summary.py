from __future__ import annotations

import json


def cap_summary_list_field(
    payload: dict,
    field: str,
    limit: int,
    truncated_key: str,
    total_key: str,
    limit_key: str,
) -> list | None:
    values = payload.get(field)
    if not isinstance(values, list):
        return None
    cap = max(1, int(limit))
    total = len(values)
    payload[limit_key] = cap
    payload[total_key] = total
    if total > cap:
        selected = values[:cap]
        payload[field] = selected
        payload[truncated_key] = True
        return selected
    payload[truncated_key] = False
    return values


def cap_run_test_feedback_files(tests: list, limit: int) -> list:
    cap = max(1, int(limit))
    normalized: list = []
    for raw in tests:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        row = dict(raw)
        feedback_files = row.get("feedback_files")
        if isinstance(feedback_files, list):
            total = len(feedback_files)
            row["feedback_files_limit"] = cap
            row["feedback_files_total"] = total
            if total > cap:
                row["feedback_files"] = feedback_files[:cap]
                row["feedback_files_truncated"] = True
            else:
                row["feedback_files_truncated"] = False
        normalized.append(row)
    return normalized


def truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or "")
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"... [truncated; showing first {cap} characters]", True


def normalize_diagnostics_for_db(entries: list, message_limit: int) -> list[dict]:
    normalized: list[dict] = []
    cap = max(1, int(message_limit))
    for raw in entries:
        item = raw if isinstance(raw, dict) else {"message": str(raw or "")}
        msg, msg_truncated = truncate_inline_text(str(item.get("message") or ""), cap)
        row = dict(item)
        row["message"] = msg
        row["message_truncated"] = bool(msg_truncated)
        row["message_limit"] = cap
        row.setdefault("level", "error")
        row.setdefault("file", "")
        row.setdefault("line", 0)
        row.setdefault("column", 0)
        row.setdefault("can_link", False)
        normalized.append(row)
    return normalized


def compact_inline_error(raw: object, *, max_chars: int = 240) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def summary_for_db(
    summary: dict,
    *,
    tests_limit: int,
    diagnostics_limit: int,
    feedback_files_limit: int,
    diagnostic_message_limit: int,
) -> str:
    payload = dict(summary)
    tests = cap_summary_list_field(
        payload,
        "tests",
        tests_limit,
        "tests_truncated",
        "tests_total",
        "tests_limit",
    )
    if isinstance(tests, list):
        payload["tests"] = cap_run_test_feedback_files(tests, feedback_files_limit)
    cap_summary_list_field(
        payload,
        "compile_diagnostics",
        diagnostics_limit,
        "compile_diagnostics_truncated",
        "compile_diagnostics_total",
        "compile_diagnostics_limit",
    )
    diagnostics = payload.get("compile_diagnostics")
    if isinstance(diagnostics, list):
        payload["compile_diagnostics"] = normalize_diagnostics_for_db(
            diagnostics,
            diagnostic_message_limit,
        )
    return json.dumps(payload)


