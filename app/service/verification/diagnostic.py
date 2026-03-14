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
        normalized.append(row)
    return normalized


def judge_backend_compile_detail(summary_obj: dict, run_root: Path) -> str:
    diagnostics = summary_obj.get("compile_diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            file_token = str(item.get("file") or "").strip()
            try:
                line_no = int(item.get("line") or 0)
            except Exception:
                line_no = 0
            try:
                col_no = int(item.get("column") or 0)
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

    rel_compile_log = str(summary_obj.get("compile_log") or "").strip()
    for rel in [rel_compile_log, "compile.log"]:
        safe_rel = str(rel or "").strip()
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

    fallback = str(summary_obj.get("error") or "").strip()
    if fallback:
        return compact_single_line(fallback, 360)
    return ""


def collect_diagnostics(snapshot: Path, text: str, diag_re) -> list[dict]:
    rows: list[dict] = []
    safe_text = str(text or "")
    for raw_line in safe_text.splitlines():
        line = str(raw_line or "").rstrip("\n")
        m = diag_re.match(line)
        if m is None:
            continue
        file_token = str(m.group("file") or "").strip()
        try:
            file_path = (snapshot / file_token).resolve()
            if snapshot not in file_path.parents:
                file_token = ""
        except Exception:
            file_token = ""
        try:
            row_line = int(m.group("line") or 0)
        except Exception:
            row_line = 0
        try:
            row_col = int(m.group("col") or 0)
        except Exception:
            row_col = 0
        rows.append(
            {
                "file": file_token,
                "line": row_line,
                "column": row_col,
                "level": str(m.group("level") or "").strip().lower(),
                "message": str(m.group("msg") or "").strip(),
            }
        )
    return rows
