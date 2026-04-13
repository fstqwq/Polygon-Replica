from __future__ import annotations

import base64
import uuid
from pathlib import Path

from app.impl.runtime.config import config
from app.service.platform.error_text import bounded_display_text, normalize_display_text
from app.service.platform.testlib_source import workspace_testlib_header

_C = config.constants
_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".c"}


def _first_compile_message(summary: dict[str, object]) -> str:
    diagnostics = summary.get("compile_diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, str):
            continue
        message = message.strip()
        if message:
            return message
    error = summary.get("error")
    return error.strip() if isinstance(error, str) else ""


def _compile_error_text(summary: dict[str, object]) -> str:
    diagnostics = summary.get("compile_diagnostics")
    if not isinstance(diagnostics, list):
        diagnostics = []
    lines: list[str] = []
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, str):
            continue
        message = message.strip()
        if not message:
            continue
        file_token = item.get("file")
        if isinstance(file_token, str):
            file_token = file_token.strip()
        else:
            file_token = ""
        line_value = item.get("line")
        try:
            line_no = int(line_value) if line_value is not None else 0
        except Exception:
            line_no = 0
        column_value = item.get("column")
        try:
            col_no = int(column_value) if column_value is not None else 0
        except Exception:
            col_no = 0
        prefix = ""
        if file_token and line_no > 0 and col_no > 0:
            prefix = f"{file_token}:{line_no}:{col_no}: "
        elif file_token and line_no > 0:
            prefix = f"{file_token}:{line_no}: "
        elif file_token:
            prefix = f"{file_token}: "
        text = normalize_display_text(prefix + message)
        if text:
            lines.append(text)
    if lines:
        return bounded_display_text("\n".join(lines))
    fallback = summary.get("error")
    if isinstance(fallback, str):
        fallback = fallback.strip()
    else:
        fallback = ""
    if fallback:
        return bounded_display_text(fallback)
    return ""


def _run_summary_verdict(summary: dict[str, object]) -> str:
    tests = summary.get("tests")
    if not isinstance(tests, list):
        tests = []
    for row in tests:
        if not isinstance(row, dict):
            continue
        verdict = row.get("verdict")
        if isinstance(verdict, str):
            verdict = verdict.strip().upper()
        else:
            verdict = ""
        if verdict:
            return verdict
        passes = row.get("passes")
        if not isinstance(passes, list):
            passes = []
        for pass_row in passes:
            if not isinstance(pass_row, dict):
                continue
            verdict = pass_row.get("verdict")
            if isinstance(verdict, str):
                verdict = verdict.strip().upper()
            else:
                verdict = ""
            if verdict:
                return verdict
    return ""


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
    verification_source: str,
) -> str:
    safe_source_path = source_path.strip()

    def _with_path(msg: str) -> str:
        message = msg.strip()
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

    source_bytes = source_content.encode("utf-8")
    source_name = Path(safe_source_path).name or "submission.cpp"
    run_id = f"r-cchk-{uuid.uuid4().hex[:12]}"
    verification_id = f"ver-compilecheck-{uuid.uuid4().hex[:12]}"
    prepared_payload = _testlib_extra_sources(workspace, safe_source_path)
    backend_error = ""
    result_obj: dict[str, object] = {}
    try:
        returned = config.judgehost_task_service.compile_only_submission(
            problem=problem,
            username=user,
            artifact_verification_id=str(getattr(_C, "RUN_PLACEHOLDER_VERIFICATION_ID", "pending")),
            upload_content=source_bytes,
            upload_filename=source_name,
            run_id=run_id,
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source=verification_source.strip() or "problem.save_source",
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )
        if isinstance(returned, dict):
            result_obj = dict(returned)
    except Exception as exc:
        backend_error = str(exc).strip()

    summary = result_obj.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    else:
        summary = dict(summary)
    task_status = result_obj.get("task_status")
    if isinstance(task_status, str):
        task_status = task_status.strip().lower()
    else:
        task_status = ""
    run_status = result_obj.get("status")
    if isinstance(run_status, str):
        run_status = run_status.strip().lower()
    else:
        run_status = ""

    if task_status == "failed" or (run_status and run_status != "ok"):
        result_error = result_obj.get("error")
        if isinstance(result_error, str):
            result_error = result_error.strip()
        else:
            result_error = ""
        summary_error = summary.get("error")
        if isinstance(summary_error, str):
            summary_error = summary_error.strip()
        else:
            summary_error = ""
        message = (
            _compile_error_text(summary)
            or _first_compile_message(summary)
            or result_error
            or summary_error
            or backend_error
            or "judge backend compile failed"
        )
        return _with_path(message or "compile check failed")

    verdict = _run_summary_verdict(summary)
    if verdict and verdict != "OK":
        message = _compile_error_text(summary) or _first_compile_message(summary) or f"judge backend compile failed ({verdict})"
        return _with_path(message or "compile check failed")
    if backend_error:
        return _with_path(bounded_display_text(backend_error) or "compile check failed")
    return ""

