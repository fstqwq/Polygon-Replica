from __future__ import annotations

from pathlib import Path


def truncate_inline_text(value: str, max_chars: int) -> tuple[str, bool]:
    cap = max(1, int(max_chars))
    text = str(value or "")
    if len(text) <= cap:
        return text, False
    return text[:cap] + f"... [truncated; showing first {cap} characters]", True


def compact_single_line(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    cap = max(1, int(max_chars))
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "..."


def normalize_diagnostics_for_db(entries: list[dict], message_limit: int) -> list[dict]:
    normalized: list[dict] = []
    cap = max(1, int(message_limit))
    for item in entries:
        message = item.get("message") or ""
        msg, msg_truncated = truncate_inline_text(message, cap)
        row = dict(item)
        row["message"] = msg
        row["message_truncated"] = bool(msg_truncated)
        row["message_limit"] = cap
        normalized.append(row)
    return normalized


def judge_backend_compile_detail(summary_obj: dict, run_root: Path) -> str:
    diagnostics = summary_obj.get("compile_diagnostics")
    if diagnostics is not None:
        for item in diagnostics:
            message = (item.get("message") or "").strip()
            if not message:
                continue
            file_token = (item.get("file") or "").strip()
            try:
                line_no = int(item.get("line", 0))
            except Exception:
                line_no = 0
            try:
                col_no = int(item.get("column", 0))
            except Exception:
                col_no = 0
            prefix = ""
            if file_token and line_no > 0 and col_no > 0:
                prefix = f"{file_token}:{line_no}:{col_no}: "
            elif file_token and line_no > 0:
                prefix = f"{file_token}:{line_no}: "
            elif file_token:
                prefix = f"{file_token}: "
            return compact_single_line(prefix + message, 360)

    for rel in [(summary_obj.get("compile_log") or "").strip(), "compile.log"]:
        safe_rel = rel.strip()
        if not safe_rel:
            continue
        try:
            candidate = (run_root / safe_rel).resolve()
        except Exception:
            continue
        try:
            if candidate != run_root and run_root not in candidate.parents:
                continue
        except Exception:
            continue
        if not candidate.exists() or (not candidate.is_file()):
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        compact = compact_single_line(text, 360)
        if compact:
            return compact

    fallback = (summary_obj.get("error") or "").strip()
    if fallback:
        return compact_single_line(fallback, 360)
    return ""

