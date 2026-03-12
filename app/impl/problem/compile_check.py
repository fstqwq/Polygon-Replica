from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path

from app.impl.runtime.config import config
from app.service.platform.error_text import compact_error_text, preserve_error_text
from app.service.platform.testlib_source import workspace_testlib_header

_C = config.constants
_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".c"}
_RUN_INFLIGHT_STATUSES = {"", "queued", "pending", "running"}


def _first_compile_message(summary: dict[str, object]) -> str:
    diagnostics_obj = summary.get("compile_diagnostics")
    diagnostics = diagnostics_obj if isinstance(diagnostics_obj, list) else []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            return message
    return str(summary.get("error") or "").strip()


def _compile_error_text(summary: dict[str, object]) -> str:
    diagnostics_obj = summary.get("compile_diagnostics")
    diagnostics = diagnostics_obj if isinstance(diagnostics_obj, list) else []
    lines: list[str] = []
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
        text = preserve_error_text(prefix + message, max_chars=1200, max_lines=16)
        if text:
            lines.append(text)
    if lines:
        return preserve_error_text("\n".join(lines), max_chars=1200, max_lines=16)
    fallback = str(summary.get("error") or "").strip()
    if fallback:
        return preserve_error_text(fallback, max_chars=1200, max_lines=16)
    return ""


def _summary_needs_compile_detail(summary: dict[str, object]) -> bool:
    diagnostics_obj = summary.get("compile_diagnostics")
    diagnostics = diagnostics_obj if isinstance(diagnostics_obj, list) else []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if message:
            return False
    error_text = str(summary.get("error") or "").strip().lower()
    if "compiler output" in error_text:
        return True
    return not bool(error_text)


def _run_summary_verdict(summary: dict[str, object]) -> str:
    tests_obj = summary.get("tests")
    tests = tests_obj if isinstance(tests_obj, list) else []
    for row in tests:
        if not isinstance(row, dict):
            continue
        verdict = str(row.get("verdict") or "").strip().upper()
        if verdict:
            return verdict
        passes_obj = row.get("passes")
        passes = passes_obj if isinstance(passes_obj, list) else []
        for pass_row in passes:
            if not isinstance(pass_row, dict):
                continue
            verdict = str(pass_row.get("verdict") or "").strip().upper()
            if verdict:
                return verdict
    return ""


def _run_status_needs_wait(run_status: str, summary: dict[str, object]) -> bool:
    safe_status = str(run_status or "").strip().lower()
    if safe_status in _RUN_INFLIGHT_STATUSES:
        return True
    if safe_status == "ok":
        return False
    return _summary_needs_compile_detail(summary)


def _testlib_extra_sources(workspace: Path, source_path: str) -> dict[str, object] | None:
    if Path(source_path).suffix.lower() not in _CPP_EXTENSIONS:
        return None
    testlib_header = workspace_testlib_header(workspace)
    if testlib_header is not None:
        blob = testlib_header.read_bytes()
        return {"extra_sources_b64": {"testlib.h": base64.b64encode(blob).decode("ascii")}}
    return None


def judgehost_compile_check_error(
    *,
    problem: str,
    user: str,
    workspace: Path,
    source_path: str,
    source_content: str,
    invocation_source: str,
) -> str:
    safe_source_path = str(source_path or "").strip()

    def _with_path(msg: str) -> str:
        message = str(msg or "").strip()
        if not message:
            return message
        if safe_source_path:
            return f"{safe_source_path}: {message}"
        return message

    backend = config.judgehost_task_service
    try:
        if (not backend.enabled()) or (not backend.auth_token_configured()):
            return _with_path("judge backend unavailable for compile check")
        status_obj = backend.status()
        hosts_online = int(status_obj.get("hosts_online", 0)) if isinstance(status_obj, dict) else 0
        if hosts_online <= 0:
            return _with_path("judge backend offline for compile check")
    except Exception:
        return _with_path("judge backend unavailable for compile check")

    source_bytes = str(source_content or "").encode("utf-8")
    source_name = Path(safe_source_path).name or "submission.cpp"
    run_id = f"r-cchk-{uuid.uuid4().hex[:12]}"
    invocation_id = f"inv-compilecheck-{uuid.uuid4().hex[:12]}"
    prepared_payload = _testlib_extra_sources(workspace, safe_source_path)
    wait_timeout_sec = max(10.0, min(120.0, float(getattr(_C, "JUDGEHOST_WAIT_TIMEOUT_SEC", 120) or 120)))
    backend_error = ""
    try:
        waited_run_id = str(
            config.invocation_backend_service.compile_only_submission(
                problem=problem,
                username=user,
                build_id=str(getattr(_C, "RUN_PLACEHOLDER_BUILD_ID", "pending") or "pending"),
                upload_content=source_bytes,
                upload_filename=source_name,
                run_id=run_id,
                invocation_id=invocation_id,
                invocation_run_ids=[run_id],
                expected_behavior="compile",
                invocation_source=str(invocation_source or "problem.save_source").strip() or "problem.save_source",
                prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
            )
            or run_id
        ).strip() or run_id
    except Exception as exc:
        waited_run_id = run_id
        backend_error = str(exc or "").strip()

    run_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [waited_run_id])
    if run_row is None:
        if backend_error:
            return _with_path(compact_error_text(backend_error, max_chars=320) or "compile check failed")
        return _with_path("judge backend compile result missing")
    run_status = str(run_row["status"] or "").strip().lower()

    def _load_summary(row_obj) -> dict[str, object]:
        summary_local: dict[str, object] = {}
        raw_summary = str(row_obj["summary_json"] or "").strip()
        if raw_summary:
            try:
                parsed = json.loads(raw_summary)
                if isinstance(parsed, dict):
                    summary_local = parsed
            except Exception:
                summary_local = {}
        return summary_local

    summary_obj = _load_summary(run_row)
    deadline = time.monotonic() + wait_timeout_sec
    while _run_status_needs_wait(run_status, summary_obj) and time.monotonic() < deadline:
        time.sleep(0.1)
        latest_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [waited_run_id])
        if latest_row is None:
            break
        run_status = str(latest_row["status"] or "").strip().lower()
        summary_obj = _load_summary(latest_row)
    if run_status and run_status != "ok":
        if _summary_needs_compile_detail(summary_obj):
            time.sleep(0.1)
            latest_row = config.db.fetch_one("SELECT status,summary_json FROM runs WHERE id=?", [waited_run_id])
            if latest_row is not None:
                run_status = str(latest_row["status"] or "").strip().lower()
                summary_obj = _load_summary(latest_row)
    if run_status and run_status != "ok":
        message = _compile_error_text(summary_obj) or _first_compile_message(summary_obj) or str(summary_obj.get("error") or "").strip() or "judge backend compile failed"
        return _with_path(message or "compile check failed")

    verdict = _run_summary_verdict(summary_obj)
    if verdict and verdict != "OK":
        message = _compile_error_text(summary_obj) or _first_compile_message(summary_obj) or f"judge backend compile failed ({verdict})"
        return _with_path(message or "compile check failed")
    if backend_error:
        return _with_path(compact_error_text(backend_error, max_chars=320) or "compile check failed")
    return ""
