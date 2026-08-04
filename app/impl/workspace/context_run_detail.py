from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict, cast

from fastapi import Request

from app.impl.runtime.config import config
from app.main_util import (
    normalize_optional_component_source_path_safe,
    sanitize_log_text_for_ui,
)

from app.impl.workspace.context_operation import dedupe_preserve_order, workspace_rel_file_exists

_C = config.constants

_RUNPIPE_PROTOCOL_TOKEN_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")


RunDetailPreview = TypedDict(
    "RunDetailPreview",
    {
        "available": bool,
        "text": str,
        "truncated": bool,
        "limit": int,
        "download_href": str,
        "message": str,
    },
)

InteractiveTranscriptRow = TypedDict(
    "InteractiveTranscriptRow",
    {
        "side": str,
        "text": str,
    },
)

InteractiveTranscriptPreview = TypedDict(
    "InteractiveTranscriptPreview",
    {
        "available": bool,
        "rows": list[InteractiveTranscriptRow],
        "shown": int,
        "total": int,
        "truncated": bool,
    },
)

DiagnosticEntry = TypedDict(
    "DiagnosticEntry",
    {
        "message": str,
        "message_truncated": bool,
        "message_limit": int,
        "level": str,
        "file": str,
        "line": int,
        "column": int,
        "can_link": bool,
        "file_display": str,
        "location_display": str,
        "location_title": str,
        "level_upper": str,
    },
    total=False,
)


def _strip_runpipe_protocol_lines(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for line in text.split("\n"):
        if _RUNPIPE_PROTOCOL_TOKEN_RE.search(line):
            continue
        kept.append(line)
    while kept and (not kept[0].strip()):
        kept.pop(0)
    while kept and (not kept[-1].strip()):
        kept.pop()
    return "\n".join(kept)

def _run_detail_preview_unavailable(message: str = 'missing') -> RunDetailPreview:
    return {'available': False, 'text': '', 'truncated': False, 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': '', 'message': message}

def _run_detail_preview_from_bytes(blob: bytes, download_href: str = "") -> RunDetailPreview:
    limit = int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES)
    data = blob
    clipped = len(data) > limit
    head = data[:limit]
    normalized = sanitize_log_text_for_ui(head.decode("utf-8", errors="replace"))
    normalized = _strip_runpipe_protocol_lines(normalized)
    if not normalized:
        normalized = "(empty)"
    return {
        "available": True,
        "text": normalized,
        "truncated": bool(clipped),
        "limit": limit,
        "download_href": download_href,
        "message": "",
    }

def _interactive_transcript_preview(preview: RunDetailPreview, *, line_limit: int = 24) -> InteractiveTranscriptPreview:
    if not preview["available"]:
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    raw_text = preview["text"]
    if (not raw_text.strip()) or raw_text.strip() == "(empty)":
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[InteractiveTranscriptRow] = []
    last_side = "right"
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        side = ""
        text = line
        if line.startswith("<"):
            side = "left"
            text = line[1:].lstrip()
        elif line.startswith(">"):
            side = "right"
            text = line[1:].lstrip()
        else:
            lower = line.lower()
            if lower.startswith("interactor:") or lower.startswith("jury:") or lower.startswith("judge:"):
                side = "left"
                text = line.split(":", 1)[1].lstrip() if ":" in line else line
            elif lower.startswith("submission:") or lower.startswith("team:") or lower.startswith("solution:"):
                side = "right"
                text = line.split(":", 1)[1].lstrip() if ":" in line else line
        if not side:
            side = "left" if last_side == "right" else "right"
        last_side = side
        if not text:
            text = line
        rows.append({"side": side, "text": text})
    if not rows:
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    cap = max(1, int(line_limit))
    shown_rows = rows[:cap]
    truncated = len(rows) > cap or preview["truncated"]
    return {
        "available": True,
        "rows": shown_rows,
        "shown": len(shown_rows),
        "total": len(rows),
        "truncated": truncated,
    }

def _nonnegative_int_or_none(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    return value if value >= 0 else None


def _positive_int_or_none(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except Exception:
        return None
    return value if value > 0 else None


def _cap_summary_list(summary: dict[str, object], field: str, limit: int, truncated_key: str, total_key: str, limit_key: str) -> None:
    values = summary.get(field)
    if values is None:
        return
    rows = cast(list[object], values)
    cap = max(1, int(limit))
    existing_total = _nonnegative_int_or_none(summary.get(total_key))
    existing_truncated = cast(bool | None, summary.get(truncated_key))
    total = len(rows)
    if existing_total is not None:
        total = max(total, existing_total)
    if len(rows) > cap:
        shown = rows[:cap]
        summary[field] = shown
    summary[limit_key] = cap
    summary[total_key] = total
    if existing_truncated is not None:
        summary[truncated_key] = existing_truncated or total > cap or len(rows) > cap
        return
    summary[truncated_key] = total > cap

def _cap_run_test_feedback_files(summary: dict[str, object], limit: int) -> None:
    tests = summary.get('tests')
    if tests is None:
        return
    test_rows = cast(list[dict[str, object]], tests)
    cap = max(1, int(limit))
    for row in test_rows:
        files = row.get('feedback_files')
        if files is None:
            continue
        feedback_files = cast(list[object], files)
        existing_total = _nonnegative_int_or_none(row.get('feedback_files_total'))
        existing_truncated = cast(bool | None, row.get('feedback_files_truncated'))
        total = len(feedback_files)
        if existing_total is not None:
            total = max(total, existing_total)
        if len(feedback_files) > cap:
            row['feedback_files'] = feedback_files[:cap]
        row['feedback_files_limit'] = cap
        row['feedback_files_total'] = total
        if existing_truncated is not None:
            row['feedback_files_truncated'] = existing_truncated or total > cap or len(feedback_files) > cap
            continue
        row['feedback_files_truncated'] = total > cap

def _truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = value
    if len(text) <= cap:
        return (text, False)
    return (text[:cap] + f'... [truncated; showing first {cap} characters]', True)

def _normalize_diagnostics(entries: list[DiagnosticEntry], message_limit: int) -> list[DiagnosticEntry]:
    normalized: list[DiagnosticEntry] = []
    for item in entries:
        message = item.get("message")
        if message is None:
            message = ""
        msg, msg_truncated = _truncate_inline_text(message, message_limit)
        persisted_truncated = item.get('message_truncated')
        if persisted_truncated is None:
            persisted_truncated = False
        persisted_limit = _positive_int_or_none(item.get('message_limit'))
        row: DiagnosticEntry = dict(item)
        row['message'] = msg
        row['message_truncated'] = msg_truncated or persisted_truncated
        if msg_truncated:
            row['message_limit'] = message_limit
        elif persisted_truncated and persisted_limit is not None:
            row['message_limit'] = persisted_limit
        else:
            row['message_limit'] = message_limit
        level = row.get('level')
        row['level'] = level if level is not None and level.strip() else 'error'
        file_path = row.get('file')
        row['file'] = '' if file_path is None else file_path
        line_value = _nonnegative_int_or_none(row.get('line'))
        row['line'] = 0 if line_value is None else line_value
        column_value = _nonnegative_int_or_none(row.get('column'))
        row['column'] = 0 if column_value is None else column_value
        can_link = row.get('can_link')
        row['can_link'] = False if can_link is None else bool(can_link)
        normalized.append(row)
    return normalized

def _diagnostic_file_display(file_path: str) -> str:
    text = file_path.strip()
    if not text:
        return ''
    normalized = text.replace('\\', '/')
    normalized = re.sub('/run-[A-Za-z0-9._-]+/', '/run/', normalized)
    is_absolute_like = normalized.startswith('/') or bool(re.match('^[A-Za-z]:/', normalized))
    if not is_absolute_like:
        return normalized
    pieces = [part for part in normalized.split('/') if part]
    if not pieces:
        return normalized
    if len(pieces) >= 2:
        return '/'.join(pieces[-2:])
    return pieces[-1]

def _decorate_compile_diagnostics(entries: list[DiagnosticEntry]) -> list[DiagnosticEntry]:
    decorated: list[DiagnosticEntry] = []
    for item in entries:
        row: DiagnosticEntry = dict(item)
        file_text = row.get("file")
        if file_text is None:
            file_text = ""
        file_display = _diagnostic_file_display(file_text)
        line_value = _nonnegative_int_or_none(row.get("line"))
        if line_value is None:
            line_value = 0
        column_value = _nonnegative_int_or_none(row.get("column"))
        if column_value is None:
            column_value = 0
        location_display = file_display
        if not location_display:
            location_display = '(unknown)'
        if line_value > 0:
            location_display += f':{line_value}'
            if column_value > 0:
                location_display += f':{column_value}'
        location_title = location_display
        row['file_display'] = file_display if file_display else '(unknown)'
        row['location_display'] = location_display
        row['location_title'] = location_title if location_title else location_display
        level = row.get("level")
        if level is None:
            level = "error"
        else:
            level = level.strip()
        if not level:
            level = 'error'
        row['level'] = level
        row['level_upper'] = level.upper()
        decorated.append(row)
    return decorated

def normalize_run_id_token(raw: str | None) -> str:
    if raw is None:
        return ''
    token = raw.strip()
    if not token:
        return ''
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', token):
        return ''
    return token

def normalize_run_test_name_token(raw: str | None) -> str:
    if raw is None:
        return ''
    token = raw.strip()
    if not token:
        return ''
    if not _C.RUN_TEST_NAME_RE.fullmatch(token):
        return ''
    return token

def parse_run_test_names(raw_values: list[str] | None) -> list[str]:
    if raw_values is None:
        return []
    result: list[str] = []
    for raw in raw_values:
        token = normalize_run_test_name_token(raw)
        if token:
            result.append(token)
    return dedupe_preserve_order(result)

def parse_verification_detail_id(request: Request) -> str:
    for raw in request.query_params.getlist('verification_id'):
        token = normalize_run_id_token(raw)
        if token:
            return token
    return ''


def _run_source_from_summary(summary: dict[str, object] | None) -> str:
    if summary is None:
        return ''
    source = cast(str | None, summary.get("source"))
    if source is None:
        return ""
    return source

def _run_rejudge_source_context(source: str, workspace: Path) -> tuple[str, str]:
    source_text = source.strip()
    if not source_text:
        return ('', 'run source missing')
    safe_solution = normalize_optional_component_source_path_safe(source_text, 'solutions', 'solution path')
    if not safe_solution:
        return ('', 'source is upload or outside solutions/')
    if not workspace_rel_file_exists(workspace, safe_solution):
        return ('', f'source file missing in current workspace ({safe_solution})')
    return (safe_solution, '')

def _summarize_rejudge_unavailable_reason(reasons: list[str]) -> str:
    unique_reasons = dedupe_preserve_order([item.strip() for item in reasons if item.strip()])
    if not unique_reasons:
        return 'no reusable solutions source'
    if len(unique_reasons) <= 2:
        return '; '.join(unique_reasons)
    hidden = len(unique_reasons) - 2
    return f'{unique_reasons[0]}; {unique_reasons[1]}; +{hidden} more'

def _run_rejudge_context_for_entries(entries: list[dict[str, object]], workspace: Path) -> dict[str, str | list[str]]:
    if not entries:
        return {'paths': [], 'unavailable_reason': 'no reusable solutions source'}
    statuses = [
        cast(str | None, item.get("status")) if item.get("status") is not None else ""
        for item in entries
    ]
    if any((status == 'running' for status in statuses)):
        return {'paths': [], 'unavailable_reason': 'verification still running'}
    reusable_paths: list[str] = []
    unavailable_reasons: list[str] = []
    all_reusable = True
    for item in entries:
        source = cast(str | None, item.get("source")) if item.get("source") is not None else ""
        safe_solution, unavailable_reason = _run_rejudge_source_context(source, workspace)
        if safe_solution:
            reusable_paths.append(safe_solution)
        else:
            all_reusable = False
            if unavailable_reason:
                unavailable_reasons.append(unavailable_reason)
    deduped_paths = dedupe_preserve_order(reusable_paths)
    if all_reusable and deduped_paths:
        return {
            'paths': deduped_paths,
            'unavailable_reason': '',
        }
    return {
        'paths': [],
        'unavailable_reason': _summarize_rejudge_unavailable_reason(unavailable_reasons),
    }

def _verification_status_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    statuses = [
        cast(str | None, item.get("status")) if item.get("status") is not None else ""
        for item in entries
    ]
    has_running = any((status in {'running', 'queued', 'pending'} for status in statuses))
    matched_count = sum((1 for item in entries if bool(item.get('matched'))))
    total_count = len(entries)
    if has_running:
        status_text = 'running'
    else:
        status_text = 'ok' if total_count > 0 and matched_count == total_count else 'failed'
    return {
        'status': status_text,
        'is_failed': status_text == 'failed',
        'has_running': has_running,
        'matched_count': matched_count,
        'total_count': total_count,
    }

