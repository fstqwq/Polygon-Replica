from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Request

from app.impl.runtime.config import config
from app.main_util import (
    normalize_optional_component_source_path_safe,
    sanitize_log_text_for_ui,
)

from .context_operation import dedupe_preserve_order, _file_head_text, workspace_rel_file_exists

_C = config.constants

_RUNPIPE_PROTOCOL_TOKEN_RE = re.compile(r"\[\s*[0-9]+(?:\.[0-9]+)?s/[0-9]+\]")
def _strip_runpipe_protocol_lines(raw: str) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
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

def _run_detail_preview_unavailable(message: str='missing') -> dict[str, object]:
    return {'available': False, 'text': '', 'truncated': False, 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': '', 'message': str(message or 'missing')}

def _run_detail_preview_from_path(path: Path, download_href: str) -> dict[str, object]:
    text, clipped = _file_head_text(path, _C.RUN_DETAIL_PREVIEW_MAX_BYTES)
    normalized = sanitize_log_text_for_ui(text)
    normalized = _strip_runpipe_protocol_lines(normalized)
    if not normalized:
        normalized = '(empty)'
    return {'available': True, 'text': normalized, 'truncated': bool(clipped), 'limit': int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES), 'download_href': str(download_href or ''), 'message': ''}


def _run_detail_preview_from_bytes(blob: bytes, download_href: str = "") -> dict[str, object]:
    limit = int(_C.RUN_DETAIL_PREVIEW_MAX_BYTES)
    data = bytes(blob or b"")
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
        "download_href": str(download_href or ""),
        "message": "",
    }

def _run_detail_preview_is_noise(preview: dict[str, object]) -> bool:
    if not isinstance(preview, dict):
        return True
    if not bool(preview.get("available")):
        return True
    text = str(preview.get("text") or "").strip()
    if not text or text == "(empty)":
        return True
    return bool(_RUNPIPE_PROTOCOL_TOKEN_RE.search(text))

def _interactive_transcript_preview(preview: dict[str, object], *, line_limit: int = 24) -> dict[str, object]:
    if not isinstance(preview, dict) or (not bool(preview.get("available"))):
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    raw_text = str(preview.get("text") or "")
    if (not raw_text.strip()) or raw_text.strip() == "(empty)":
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict[str, str]] = []
    last_side = "right"
    for raw_line in lines:
        line = str(raw_line or "").strip()
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
        rows.append({"side": side, "text": text or line})
    if not rows:
        return {"available": False, "rows": [], "shown": 0, "total": 0, "truncated": False}
    cap = max(1, int(line_limit))
    shown_rows = rows[:cap]
    truncated = bool(len(rows) > cap or preview.get("truncated"))
    return {
        "available": True,
        "rows": shown_rows,
        "shown": len(shown_rows),
        "total": len(rows),
        "truncated": truncated,
    }

def _cap_summary_list(summary: dict, field: str, limit: int, truncated_key: str, total_key: str, limit_key: str) -> None:
    values = summary.get(field)
    if not isinstance(values, list):
        return

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value >= 0 else None
    cap = max(1, int(limit))
    existing_total = _int_or_none(summary.get(total_key))
    existing_truncated = summary.get(truncated_key) if isinstance(summary.get(truncated_key), bool) else None
    total = len(values)
    if existing_total is not None:
        total = max(total, existing_total)
    shown = values
    if len(values) > cap:
        shown = values[:cap]
        summary[field] = shown
    summary[limit_key] = cap
    summary[total_key] = total
    if existing_truncated is not None:
        summary[truncated_key] = bool(existing_truncated) or total > cap or len(values) > cap
        return
    summary[truncated_key] = total > cap

def _cap_run_test_feedback_files(summary: dict, limit: int) -> None:
    tests = summary.get('tests')
    if not isinstance(tests, list):
        return

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value >= 0 else None
    cap = max(1, int(limit))
    for row in tests:
        if not isinstance(row, dict):
            continue
        files = row.get('feedback_files')
        if not isinstance(files, list):
            continue
        existing_total = _int_or_none(row.get('feedback_files_total'))
        existing_truncated = row.get('feedback_files_truncated') if isinstance(row.get('feedback_files_truncated'), bool) else None
        total = len(files)
        if existing_total is not None:
            total = max(total, existing_total)
        if len(files) > cap:
            row['feedback_files'] = files[:cap]
        row['feedback_files_limit'] = cap
        row['feedback_files_total'] = total
        if existing_truncated is not None:
            row['feedback_files_truncated'] = bool(existing_truncated) or total > cap or len(files) > cap
            continue
        row['feedback_files_truncated'] = total > cap

def _truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or '')
    if len(text) <= cap:
        return (text, False)
    return (text[:cap] + f'... [truncated; showing first {cap} characters]', True)

def _normalize_diagnostics(entries: list, message_limit: int) -> list[dict]:

    def _int_or_none(raw) -> int | None:
        try:
            value = int(raw)
        except Exception:
            return None
        return value if value > 0 else None
    normalized: list[dict] = []
    for raw in entries:
        item = raw if isinstance(raw, dict) else {'message': str(raw or '')}
        msg, msg_truncated = _truncate_inline_text(str(item.get('message') or ''), message_limit)
        persisted_truncated = bool(item.get('message_truncated')) if isinstance(item, dict) else False
        persisted_limit = _int_or_none(item.get('message_limit')) if isinstance(item, dict) else None
        row = dict(item)
        row['message'] = msg
        row['message_truncated'] = bool(msg_truncated) or persisted_truncated
        if msg_truncated:
            row['message_limit'] = message_limit
        elif persisted_truncated and persisted_limit is not None:
            row['message_limit'] = persisted_limit
        else:
            row['message_limit'] = message_limit
        row.setdefault('level', 'error')
        row.setdefault('file', '')
        row.setdefault('line', 0)
        row.setdefault('column', 0)
        row.setdefault('can_link', False)
        normalized.append(row)
    return normalized

def _diagnostic_file_display(file_path: str) -> str:
    text = str(file_path or '').strip()
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

def _decorate_compile_diagnostics(entries: list[dict]) -> list[dict]:
    decorated: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        file_text = str(row.get('file') or '').strip()
        file_display = _diagnostic_file_display(file_text)
        try:
            line_value = max(0, int(row.get('line') or 0))
        except Exception:
            line_value = 0
        try:
            column_value = max(0, int(row.get('column') or 0))
        except Exception:
            column_value = 0
        location_display = file_display or '(unknown)'
        if line_value > 0:
            location_display += f':{line_value}'
            if column_value > 0:
                location_display += f':{column_value}'
        location_title = location_display
        row['file_display'] = file_display or '(unknown)'
        row['location_display'] = location_display
        row['location_title'] = location_title or location_display
        level = str(row.get('level') or 'error').strip().lower()
        if not level:
            level = 'error'
        row['level'] = level
        row['level_upper'] = level.upper()
        decorated.append(row)
    return decorated

def normalize_run_id_token(raw: str | None) -> str:
    token = str(raw or '').strip()
    if not token:
        return ''
    if not re.fullmatch('[A-Za-z0-9._-]{1,80}', token):
        return ''
    return token

def normalize_run_test_name_token(raw: str | None) -> str:
    token = str(raw or '').strip()
    if not token:
        return ''
    if not _C.RUN_TEST_NAME_RE.fullmatch(token):
        return ''
    return token

def parse_run_test_names(raw_values: object) -> list[str]:
    values: list[str] = []
    if raw_values is None:
        return values
    if isinstance(raw_values, str):
        values.append(raw_values)
    elif isinstance(raw_values, list):
        values.extend((str(item or '') for item in raw_values))
    elif isinstance(raw_values, tuple):
        values.extend((str(item or '') for item in raw_values))
    else:
        try:
            values.extend((str(item or '') for item in list(raw_values)))
        except Exception:
            values.append(str(raw_values or ''))
    result: list[str] = []
    for raw in values:
        token = normalize_run_test_name_token(raw)
        if token:
            result.append(token)
    return dedupe_preserve_order(result)

def parse_run_detail_ids(request: Request) -> list[str]:
    values: list[str] = []
    for raw in request.query_params.getlist('run_id'):
        token = normalize_run_id_token(raw)
        if token:
            values.append(token)
    for csv_raw in request.query_params.getlist('run_ids'):
        text = str(csv_raw or '').strip()
        if not text:
            continue
        for part in text.split(','):
            token = normalize_run_id_token(part)
            if token:
                values.append(token)
    return dedupe_preserve_order(values)

def parse_run_detail_invocation_id(request: Request) -> str:
    for raw in request.query_params.getlist('invocation_id'):
        token = normalize_run_id_token(raw)
        if token:
            return token
    return ''

def _run_source_from_summary(summary: dict | None) -> str:
    if not isinstance(summary, dict):
        return ''
    return str(summary.get('source') or '').strip()

def _run_rejudge_source_context(source: str, workspace: Path) -> tuple[str, str]:
    source_text = str(source or '').strip()
    if not source_text:
        return ('', 'run source missing')
    safe_solution = normalize_optional_component_source_path_safe(source_text, 'solutions', 'solution path')
    if not safe_solution:
        return ('', 'source is upload or outside solutions/')
    if not workspace_rel_file_exists(workspace, safe_solution):
        return ('', f'source file missing in current workspace ({safe_solution})')
    return (safe_solution, '')

def _summarize_rejudge_unavailable_reason(reasons: list[str]) -> str:
    unique_reasons = dedupe_preserve_order([str(item or '').strip() for item in reasons if str(item or '').strip()])
    if not unique_reasons:
        return 'no reusable solutions source'
    if len(unique_reasons) <= 2:
        return '; '.join(unique_reasons)
    hidden = len(unique_reasons) - 2
    return f'{unique_reasons[0]}; {unique_reasons[1]}; +{hidden} more'

def _run_rejudge_context_for_entries(entries: list[dict[str, object]], workspace: Path) -> dict[str, str | list[str]]:
    if not entries:
        return {'paths': [], 'query': '', 'unavailable_reason': 'no reusable solutions source'}
    statuses = [str(item.get('status') or '').strip().lower() for item in entries if isinstance(item, dict)]
    if any((status in {'running', 'queued', 'pending'} for status in statuses)):
        return {'paths': [], 'query': '', 'unavailable_reason': 'invocation still running'}
    reusable_paths: list[str] = []
    unavailable_reasons: list[str] = []
    all_reusable = True
    for item in entries:
        if not isinstance(item, dict):
            continue
        source = str(item.get('source') or '').strip()
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
            'query': '&'.join((f'solution_paths={quote_plus(path)}' for path in deduped_paths)),
            'unavailable_reason': '',
        }
    return {
        'paths': [],
        'query': '',
        'unavailable_reason': _summarize_rejudge_unavailable_reason(unavailable_reasons),
    }

def _run_invocation_status_summary(entries: list[dict[str, object]]) -> dict[str, object]:
    statuses = [str(item.get('status') or '').strip().lower() for item in entries if isinstance(item, dict)]
    has_running = any((status in {'running', 'queued', 'pending'} for status in statuses))
    matched_count = sum((1 for item in entries if isinstance(item, dict) and bool(item.get('matched'))))
    total_count = len(entries)
    if has_running:
        status_text = 'running'
    else:
        status_text = 'ok' if total_count > 0 and matched_count == total_count else 'failed'
    return {
        'status': status_text,
        'status_upper': status_text.upper(),
        'is_failed': status_text == 'failed',
        'has_running': has_running,
        'matched_count': matched_count,
        'total_count': total_count,
    }




