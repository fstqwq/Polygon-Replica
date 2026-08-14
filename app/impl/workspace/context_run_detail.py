import app.main_constant as _K

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict, cast

from fastapi import Request

from app.impl.runtime.dependency import runtime
from app.main_util import (
    normalize_optional_component_source_path_safe,
    sanitize_log_text_for_ui,
)

from app.impl.workspace.context_operation import dedupe_preserve_order, workspace_rel_file_exists


RunDetailPreview = TypedDict(
    "RunDetailPreview",
    {
        "available": bool,
        "text": str,
        "truncated": bool,
        "limit": int,
        "download_verification_id": str,
        "download_rel_path": str,
        "message": str,
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


def _run_detail_preview_unavailable(message: str = 'missing') -> RunDetailPreview:
    return {
        'available': False,
        'text': '',
        'truncated': False,
        'limit': runtime().config_values.integer("RUN_DETAIL_PREVIEW_MAX_BYTES"),
        'download_verification_id': '',
        'download_rel_path': '',
        'message': message,
    }

def _run_detail_preview_from_bytes(
    blob: bytes,
    *,
    verification_id: str = "",
    rel_path: str = "",
) -> RunDetailPreview:
    limit = runtime().config_values.integer("RUN_DETAIL_PREVIEW_MAX_BYTES")
    data = blob
    clipped = len(data) > limit
    head = data[:limit]
    normalized = sanitize_log_text_for_ui(head.decode("utf-8", errors="replace"))
    if not normalized:
        normalized = "(empty)"
    return {
        "available": True,
        "text": normalized,
        "truncated": bool(clipped),
        "limit": limit,
        "download_verification_id": verification_id,
        "download_rel_path": rel_path,
        "message": "",
    }

def _nonnegative_int_or_none(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return None
    else:
        return None
    return value if value >= 0 else None


def _positive_int_or_none(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return None
    else:
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

def _normalize_diagnostics(
    entries: Sequence[Mapping[str, object]],
    message_limit: int,
) -> list[DiagnosticEntry]:
    normalized: list[DiagnosticEntry] = []
    for item in entries:
        message_raw = item.get("message")
        if message_raw is not None and not isinstance(message_raw, str):
            raise RuntimeError("diagnostic message must be text")
        message = message_raw or ""
        msg, msg_truncated = _truncate_inline_text(message, message_limit)
        persisted_truncated = item.get('message_truncated')
        if persisted_truncated is None:
            persisted_truncated = False
        elif not isinstance(persisted_truncated, bool):
            raise RuntimeError("diagnostic message_truncated must be boolean")
        persisted_limit = _positive_int_or_none(item.get('message_limit'))
        level_raw = item.get('level')
        if level_raw is not None and not isinstance(level_raw, str):
            raise RuntimeError("diagnostic level must be text")
        file_raw = item.get('file')
        if file_raw is not None and not isinstance(file_raw, str):
            raise RuntimeError("diagnostic file must be text")
        can_link_raw = item.get('can_link')
        if can_link_raw is not None and not isinstance(can_link_raw, bool):
            raise RuntimeError("diagnostic can_link must be boolean")
        row: DiagnosticEntry = {}
        row['message'] = msg
        row['message_truncated'] = msg_truncated or persisted_truncated
        if msg_truncated:
            row['message_limit'] = message_limit
        elif persisted_truncated and persisted_limit is not None:
            row['message_limit'] = persisted_limit
        else:
            row['message_limit'] = message_limit
        row['level'] = level_raw if level_raw is not None and level_raw.strip() else 'error'
        row['file'] = '' if file_raw is None else file_raw
        line_value = _nonnegative_int_or_none(item.get('line'))
        row['line'] = 0 if line_value is None else line_value
        column_value = _nonnegative_int_or_none(item.get('column'))
        row['column'] = 0 if column_value is None else column_value
        row['can_link'] = False if can_link_raw is None else can_link_raw
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
        row = item.copy()
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
    if not _K.RUN_TEST_NAME_RE.fullmatch(token):
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

def _run_rejudge_context_for_entries(
    entries: Sequence[Mapping[str, object]],
    workspace: Path,
) -> dict[str, str | list[str]]:
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
        source_value = item.get("source")
        source = source_value if isinstance(source_value, str) else ""
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

class VerificationStatusSummary(TypedDict):
    status: str
    is_failed: bool
    has_running: bool
    matched_count: int
    total_count: int


def _verification_status_summary(
    entries: Sequence[Mapping[str, object]],
) -> VerificationStatusSummary:
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
