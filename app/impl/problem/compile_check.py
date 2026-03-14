from __future__ import annotations

import base64
import uuid
from pathlib import Path

from app.impl.runtime.config import config
from app.service.platform.error_text import compact_error_text, preserve_error_text
from app.service.platform.testlib_source import workspace_testlib_header

_C = config.constants
_CPP_EXTENSIONS = {".cpp", ".cc", ".cxx", ".c++", ".c"}


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
    verification_id = f"ver-compilecheck-{uuid.uuid4().hex[:12]}"
    prepared_payload = _testlib_extra_sources(workspace, safe_source_path)
    backend_error = ""
    result_obj: dict[str, object] = {}
    try:
        returned = config.judgehost_task_service.compile_only_submission(
            problem=problem,
            username=user,
            artifact_verification_id=str(getattr(_C, "RUN_PLACEHOLDER_BUILD_ID", "pending") or "pending"),
            upload_content=source_bytes,
            upload_filename=source_name,
            run_id=run_id,
            verification_id=verification_id,
            verification_run_ids=[run_id],
            expected_behavior="compile",
            verification_source=str(verification_source or "problem.save_source").strip() or "problem.save_source",
            prepared_payload=dict(prepared_payload) if isinstance(prepared_payload, dict) else None,
        )
        if isinstance(returned, dict):
            result_obj = dict(returned)
    except Exception as exc:
        backend_error = str(exc or "").strip()

    summary_obj = result_obj.get("summary")
    summary = dict(summary_obj) if isinstance(summary_obj, dict) else {}
    task_status = str(result_obj.get("task_status") or "").strip().lower()
    run_status = str(result_obj.get("status") or "").strip().lower()

    if task_status == "failed" or (run_status and run_status != "ok"):
        message = (
            _compile_error_text(summary)
            or _first_compile_message(summary)
            or str(result_obj.get("error") or "").strip()
            or str(summary.get("error") or "").strip()
            or backend_error
            or "judge backend compile failed"
        )
        return _with_path(message or "compile check failed")

    verdict = _run_summary_verdict(summary)
    if verdict and verdict != "OK":
        message = _compile_error_text(summary) or _first_compile_message(summary) or f"judge backend compile failed ({verdict})"
        return _with_path(message or "compile check failed")
    if backend_error:
        return _with_path(compact_error_text(backend_error, max_chars=320) or "compile check failed")
    return ""
