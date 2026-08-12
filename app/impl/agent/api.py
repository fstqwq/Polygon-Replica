import re
from pathlib import Path
from typing import cast

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.impl.agent.shared import require_agent_token, workspace_context_for_identity
from app.impl.auth.shared import json_error_response
from app.impl.runtime.dependency import runtime
from app.impl.workspace.context_job import start_export_job, start_verification_job
from app.impl.workspace.context_job_helper import allocate_verification_id
from app.impl.workspace.context_run_detail import normalize_run_test_name_token
from app.impl.workspace.problem_config import read_problem_config
from app.service.problem_package.workflow import build_full_verification_targets
from app.impl.workspace.run_view_detail import build_run_detail_context
from app.service.execution.identity import new_run_id
from app.service.importing.archive import (
    ArchiveView,
    problem_archive_policy,
)
from app.service.importing.upload import spool_upload
from app.service.platform.git_process import run_git
from app.service.workspace.mutation import WorkspaceMutationConflict


def _json_body(payload: object, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status_code)


async def _read_json(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return cast(dict[str, object], payload)


def _agent_problem_ctx(identity) -> dict[str, object]:
    return workspace_context_for_identity(identity)


async def agent_register(request: Request, code: str):
    payload = await _read_json(request)
    try:
        result = runtime().agent_service.register_agent(
            code=code,
            agent_name=str(payload.get("agent_name") or ""),
            desktop_id=str(payload.get("desktop_id") or ""),
            init_ts=str(payload.get("init_ts") or ""),
        )
        return _json_body(result)
    except LookupError:
        return json_error_response("registration code not found", status_code=404)
    except TimeoutError:
        return json_error_response("registration code not found", status_code=404)
    except RuntimeError:
        return json_error_response("registration code already used", status_code=410)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=422)


async def agent_request_access(request: Request):
    payload = await _read_json(request)
    try:
        result = runtime().agent_service.request_problem_access(
            agent_session_id=str(payload.get("agent_session_id") or ""),
            identity_hash=str(payload.get("identity_hash") or ""),
            problem=str(payload.get("problem") or ""),
        )
        return _json_body(result)
    except PermissionError as exc:
        return json_error_response(str(exc), status_code=401)
    except LookupError:
        return json_error_response("problem not found", status_code=404)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=422)


async def agent_poll_access(request: Request, request_id: str):
    session_id = str(request.query_params.get("agent_session_id") or "")
    identity_hash = str(request.query_params.get("identity_hash") or "")
    try:
        result = runtime().agent_service.poll_access_request(
            agent_session_id=session_id,
            identity_hash=identity_hash,
            request_id=request_id,
        )
        return _json_body(result)
    except PermissionError as exc:
        return json_error_response(str(exc), status_code=401)
    except LookupError as exc:
        return json_error_response(str(exc), status_code=404)


async def agent_auth_status(request: Request):
    session_id = str(request.query_params.get("agent_session_id") or "")
    identity_hash = str(request.query_params.get("identity_hash") or "")
    try:
        result = runtime().agent_service.session_status(
            agent_session_id=session_id,
            identity_hash=identity_hash,
        )
        return _json_body(result)
    except PermissionError as exc:
        return json_error_response(str(exc), status_code=401)


async def agent_problem_create(request: Request):
    payload = await _read_json(request)
    try:
        result = runtime().agent_service.create_problem(
            agent_session_id=str(payload.get("agent_session_id") or ""),
            identity_hash=str(payload.get("identity_hash") or ""),
            problem=str(payload.get("problem") or ""),
        )
        return _json_body(result)
    except PermissionError as exc:
        return json_error_response(str(exc), status_code=401)
    except FileExistsError as exc:
        return json_error_response(str(exc), status_code=409)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=422)


async def agent_verification_start(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    verification_id = allocate_verification_id()
    try:
        targets, _accepted_source = build_full_verification_targets(workspace)
        started = start_verification_job(
            runtime(),
            identity.problem_slug,
            identity.username,
            actor_user_id=int(identity.user_id),
            problem_id=int(identity.problem_id),
            workspace_id=int(ctx["workspace"]["id"]),
            workspace_head=str(ctx["workspace"].get("head_commit") or ""),
            workspace_dirty=bool(ctx["workspace"].get("dirty")),
            targets=targets,
            verification_id=verification_id,
            workspace_path=workspace,
        )
        if not started:
            return json_error_response("verification already running", status_code=409)
        return _json_body({"verification_id": verification_id, "status": "queued"})
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_verification_status(request: Request, verification_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    snapshot = runtime().verification_service.verification_snapshot(verification_id)
    if (
        snapshot is None
        or int(snapshot["record"]["problem_id"]) != int(identity.problem_id)
        or snapshot["record"]["workspace_id"] != int(ctx["workspace"]["id"])
    ):
        return json_error_response("verification not found", status_code=404)
    runtime_summary = runtime().verification_service.verification_runtime_summary_from_tasks(
        snapshot["tasks"]
    )
    return _json_body(
        {
            "verification_id": verification_id,
            "status": str(snapshot["record"]["status"] or ""),
            "runtime_summary": runtime_summary,
        }
    )


_YAML_SIMPLE_RE = re.compile(r"^[A-Za-z0-9_./@+=,\-() ]+$")


def _plain_text(text: str, *, status_code: int = 200) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status_code, media_type="text/plain; charset=utf-8")


def _yaml_scalar(value: object) -> str:
    text = "" if value is None else str(value)
    if text == "":
        return '""'
    if (
        _YAML_SIMPLE_RE.fullmatch(text)
        and text == text.strip()
        and text.lower() not in {"null", "true", "false", "yes", "no", "on", "off"}
    ):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def _yaml_key(value: object) -> str:
    return _yaml_scalar(value)


def _compact_memory(text: str) -> str:
    return str(text or "").replace(" ", "")


def _compact_metric_text(metrics: str) -> str:
    token = str(metrics or "").strip()
    if (not token) or token == "-":
        return ""
    if token == "running":
        return "running"
    if "/" in token:
        left, right = token.split("/", 1)
        return " ".join(part for part in (left.strip(), _compact_memory(right.strip())) if part)
    return token


def _compact_result_text(result: str, metrics: str = "", memory: str = "") -> str:
    result_token = str(result or "").strip()
    metric_token = _compact_metric_text(metrics)
    memory_token = _compact_memory(memory)
    if result_token == ".." and metric_token == "running":
        return "running"
    return " ".join(part for part in (result_token, metric_token, memory_token) if part) or "-"


def _role_from_task_kind(task_kind: str) -> str:
    if task_kind == "main-correct":
        return "main"
    if task_kind == "solution-run":
        return "solution"
    return str(task_kind or "") or "-"


def _column_keys(columns: list[dict[str, object]]) -> list[str]:
    title_counts: dict[str, int] = {}
    for col in columns:
        title = str(col.get("title") or col.get("source") or col.get("id") or "column")
        title_counts[title] = title_counts.get(title, 0) + 1
    used: set[str] = set()
    values: list[str] = []
    for col in columns:
        title = str(col.get("title") or "")
        source = str(col.get("source") or "")
        raw_key = title if title and title_counts.get(title, 0) == 1 else (source or title or str(col.get("id") or "column"))
        key = raw_key
        suffix = 2
        while key in used:
            key = f"{raw_key}-{suffix}"
            suffix += 1
        used.add(key)
        values.append(key)
    return values


def _append_scalar(lines: list[str], key: str, value: object, *, indent: int = 0) -> None:
    lines.append(f"{' ' * indent}{_yaml_key(key)}: {_yaml_scalar(value)}")


def _append_diagnostics(lines: list[str], detail_ctx: dict[str, object], *, indent: int = 0) -> None:
    diagnostics: list[str] = []
    for col in cast(list[dict[str, object]], detail_ctx.get("detail_columns") or []):
        title = str(col.get("title") or col.get("source") or "")
        error = str(col.get("error_display") or col.get("error") or "").strip()
        match_reason = str(col.get("match_reason") or "").strip()
        if error:
            diagnostics.append(f"{title}: {error}" if title else error)
        elif match_reason:
            diagnostics.append(f"{title}: {match_reason}" if title else match_reason)
        for item in cast(list[dict[str, object]], col.get("compile_diagnostics") or []):
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            location = str(item.get("location_display") or "").strip()
            level = str(item.get("level_upper") or item.get("level") or "").strip()
            body = ": ".join(part for part in (location, level, message) if part)
            diagnostics.append(f"{title}: {body}" if title and body else body)
    verification_logs = cast(dict[str, object], detail_ctx.get("detail_verification_logs") or {})
    verification_error = str(verification_logs.get("error_display") or verification_logs.get("error") or "").strip()
    if verification_error:
        diagnostics.append(f"Verification: {verification_error}")
    for item in cast(list[dict[str, object]], verification_logs.get("diagnostics") or []):
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        location = str(item.get("location_display") or "").strip()
        level = str(item.get("level_upper") or item.get("level") or "").strip()
        diagnostics.append("Verification: " + ": ".join(part for part in (location, level, message) if part))
    sanity = cast(dict[str, object], detail_ctx.get("detail_sanity") or {})
    for task in cast(list[dict[str, object]], sanity.get("attention_tasks") or []):
        task_status = str(task.get("status") or "")
        messages = cast(list[dict[str, object]], task.get("messages") or [])
        for raw_message in messages:
            severity = str(raw_message.get("severity") or task_status)
            if severity not in {"warning", "failed"}:
                continue
            message = str(raw_message.get("message") or "").strip()
            if message:
                diagnostics.append(message)
        if messages or task_status not in {"warning", "failed"}:
            continue
        detail = str(task.get("detail") or "").strip()
        if detail:
            diagnostics.append(detail)
    if not diagnostics:
        lines.append(f"{' ' * indent}diagnostics: []")
        return
    lines.append(f"{' ' * indent}diagnostics:")
    for item in diagnostics:
        lines.append(f"{' ' * (indent + 2)}- {_yaml_scalar(item)}")


def _append_sanity(lines: list[str], detail_ctx: dict[str, object]) -> None:
    sanity = cast(dict[str, object], detail_ctx.get("detail_sanity") or {})
    if not bool(sanity.get("available")):
        return
    lines.append("")
    lines.append("sanity:")
    _append_scalar(lines, "status", sanity.get("status") or "unknown", indent=2)
    reason = str(sanity.get("reason") or "")
    if reason:
        _append_scalar(lines, "reason", reason, indent=2)
    _append_scalar(lines, "ran", int(sanity.get("ran_count") or 0), indent=2)
    _append_scalar(lines, "total", int(sanity.get("task_count") or 0), indent=2)
    tasks = cast(list[dict[str, object]], sanity.get("tasks") or [])
    if not tasks:
        lines.append("  checks: []")
        return
    lines.append("  checks:")
    for task in tasks:
        lines.append(f"    - name: {_yaml_scalar(task.get('name') or '')}")
        _append_scalar(lines, "label", task.get("label") or "", indent=6)
        _append_scalar(lines, "status", task.get("status") or "", indent=6)
        detail = str(task.get("detail") or "")
        if detail:
            _append_scalar(lines, "detail", detail, indent=6)
        messages = cast(list[dict[str, object]], task.get("messages") or [])
        if messages:
            lines.append("      messages:")
            for message in messages:
                lines.append(f"        - {_yaml_scalar(message.get('message') or '')}")


def _render_full_verification_yaml(verification_id: str, detail_ctx: dict[str, object]) -> str:
    columns = cast(list[dict[str, object]], detail_ctx.get("detail_columns") or [])
    rows = cast(list[dict[str, object]], detail_ctx.get("detail_rows") or [])
    keys = _column_keys(columns)
    lines: list[str] = []
    _append_scalar(lines, "verification", verification_id)
    _append_scalar(lines, "status", detail_ctx.get("detail_status") or "")
    fail_reason = str(detail_ctx.get("detail_fail_reason") or "")
    if fail_reason:
        _append_scalar(lines, "reason", fail_reason)
    lines.append("")
    lines.append("tasks:")
    task_counts = cast(dict[str, object], detail_ctx.get("detail_task_counts") or {})
    for key in ("pending", "queued", "running", "done", "failed", "cancelled"):
        _append_scalar(lines, key, int(task_counts.get(key) or 0), indent=2)
    running_tasks = cast(list[dict[str, object]], detail_ctx.get("detail_running_tasks") or [])
    lines.append("")
    if running_tasks:
        lines.append("running:")
        for task in running_tasks:
            lines.append(f"  - {_yaml_scalar(task.get('label') or '')}")
    else:
        lines.append("running: []")
    _append_sanity(lines, detail_ctx)
    lines.append("")
    lines.append("columns:")
    for index, col in enumerate(columns):
        key = keys[index]
        lines.append(f"  {_yaml_key(key)}:")
        _append_scalar(lines, "source", col.get("source") or "", indent=4)
        _append_scalar(lines, "role", _role_from_task_kind(str(col.get("task_kind") or "")), indent=4)
        _append_scalar(lines, "expected", col.get("expected_display") or "-", indent=4)
        result = _compact_result_text(
            str(col.get("got_display") or col.get("got_short") or "-"),
            str(col.get("max_time_display") or ""),
            str(col.get("max_memory_display") or ""),
        )
        _append_scalar(lines, "result", result, indent=4)
        reason = str(col.get("error_display") or col.get("error") or col.get("match_reason") or "")
        if reason:
            _append_scalar(lines, "reason", reason, indent=4)
        lines.append("    tests:")
        if rows:
            for row in rows:
                cells = cast(list[dict[str, object]], row.get("cells") or [])
                test_name = str(row.get("test_name") or row.get("display_name") or "")
                cell = cells[index] if index < len(cells) else {}
                cell_text = _compact_result_text(str(cell.get("short") or cell.get("text") or "--"), str(cell.get("metrics") or ""))
                _append_scalar(lines, test_name, cell_text, indent=6)
        else:
            lines[-1] = "    tests: {}"
        lines.append("")
    _append_diagnostics(lines, detail_ctx)
    return "\n".join(lines).rstrip() + "\n"


def _cell_detail_status(cell: dict[str, object]) -> str:
    detail = cast(dict[str, object], cell.get("detail") or {})
    final_row = cast(dict[str, object], detail.get("final_row") or {})
    if final_row:
        return _compact_result_text(
            str(final_row.get("verdict_short") or cell.get("short") or cell.get("text") or "-"),
            str(final_row.get("time_display") or ""),
            str(final_row.get("memory_display") or ""),
        )
    return _compact_result_text(str(cell.get("short") or cell.get("text") or "--"), str(cell.get("metrics") or ""))


def _cell_feedback(cell: dict[str, object]) -> str:
    detail = cast(dict[str, object], cell.get("detail") or {})
    final_row = cast(dict[str, object], detail.get("final_row") or {})
    feedback = str(final_row.get("feedback_display") or "")
    return "" if feedback == "-" else feedback


def _cell_error(cell: dict[str, object]) -> str:
    detail = cast(dict[str, object], cell.get("detail") or {})
    return str(detail.get("compile_error_display") or "")


def _cell_diagnostics(cell: dict[str, object]) -> list[str]:
    detail = cast(dict[str, object], cell.get("detail") or {})
    values: list[str] = []
    for item in cast(list[dict[str, object]], detail.get("compile_diagnostics") or []):
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        location = str(item.get("location_display") or "").strip()
        level = str(item.get("level_upper") or item.get("level") or "").strip()
        values.append(": ".join(part for part in (location, level, message) if part))
    return values


def _append_cell_detail(lines: list[str], *, col: dict[str, object], cell: dict[str, object], indent: int) -> None:
    _append_scalar(lines, "source", col.get("source") or "", indent=indent)
    _append_scalar(lines, "role", _role_from_task_kind(str(col.get("task_kind") or "")), indent=indent)
    _append_scalar(lines, "result", _cell_detail_status(cell), indent=indent)
    _append_scalar(lines, "feedback", _cell_feedback(cell), indent=indent)
    _append_scalar(lines, "error", _cell_error(cell), indent=indent)
    diagnostics = _cell_diagnostics(cell)
    if diagnostics:
        lines.append(f"{' ' * indent}diagnostics:")
        for item in diagnostics:
            lines.append(f"{' ' * (indent + 2)}- {_yaml_scalar(item)}")
    else:
        lines.append(f"{' ' * indent}diagnostics: []")


def _render_test_zoom_yaml(verification_id: str, detail_ctx: dict[str, object], *, source_filter: str = "") -> tuple[str, bool]:
    rows = cast(list[dict[str, object]], detail_ctx.get("detail_rows") or [])
    if not rows:
        return ("test detail not found\n", False)
    row = rows[0]
    columns = cast(list[dict[str, object]], detail_ctx.get("detail_columns") or [])
    keys = _column_keys(columns)
    selected: list[tuple[str, dict[str, object], dict[str, object]]] = []
    cells = cast(list[dict[str, object]], row.get("cells") or [])
    for index, col in enumerate(columns):
        source = str(col.get("source") or "")
        if source_filter and source != source_filter:
            continue
        if index >= len(cells):
            continue
        selected.append((keys[index], col, cells[index]))
    if source_filter and not selected:
        return ("source detail not found\n", False)
    lines: list[str] = []
    _append_scalar(lines, "verification", verification_id)
    _append_scalar(lines, "status", detail_ctx.get("detail_status") or "")
    _append_scalar(lines, "test", row.get("test_name") or row.get("display_name") or "")
    generate_detail = cast(dict[str, object], row.get("generate_detail") or {})
    if generate_detail:
        lines.append("")
        lines.append("generation:")
        _append_scalar(lines, "source", generate_detail.get("display_source") or generate_detail.get("source_path") or "", indent=2)
        _append_scalar(lines, "result", generate_detail.get("status_text") or "", indent=2)
        _append_scalar(lines, "feedback", generate_detail.get("feedback_display") or "", indent=2)
        _append_scalar(lines, "error", generate_detail.get("error_text") or "", indent=2)
    lines.append("")
    if source_filter:
        _key, col, cell = selected[0]
        lines.append("cell:")
        _append_scalar(lines, "title", col.get("title") or "", indent=2)
        _append_cell_detail(lines, col=col, cell=cell, indent=2)
    else:
        for key, col, cell in selected:
            lines.append(f"{_yaml_key(key)}:")
            _append_cell_detail(lines, col=col, cell=cell, indent=2)
            lines.append("")
    return ("\n".join(lines).rstrip() + "\n", True)


def _agent_verification_detail_yaml(ctx: dict[str, object], verification_id: str, *, test_name: str, source_filter: str) -> tuple[str, int]:
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    _problem_cfg, general_cfg, _statement_cfg = read_problem_config(workspace)
    detail_ctx = build_run_detail_context(
        ctx,
        str(general_cfg["mode"]),
        requested_verification_id=verification_id,
        include_row_details=bool(test_name),
        detail_test_name=test_name,
    )
    if test_name:
        body, found = _render_test_zoom_yaml(verification_id, detail_ctx, source_filter=source_filter)
        return (body, 200 if found else 404)
    return (_render_full_verification_yaml(verification_id, detail_ctx), 200)


async def agent_verification_detail(request: Request, verification_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    detail = runtime().verification_service.workspace_verification_detail(
        int(identity.problem_id),
        int(ctx["workspace"]["id"]),
        verification_id,
    )
    if detail is None:
        return _plain_text("verification not found\n", status_code=404)
    test_name = normalize_run_test_name_token(str(request.query_params.get("test_name") or ""))
    source_filter = str(request.query_params.get("source") or "")
    body, status_code = _agent_verification_detail_yaml(
        ctx,
        verification_id,
        test_name=test_name,
        source_filter=source_filter,
    )
    return _plain_text(body, status_code=status_code)


async def agent_export_start(request: Request):
    identity = require_agent_token(request, min_scope="workspace")
    payload = await _read_json(request)
    export_type = str(payload.get("export_type") or "icpc").strip().lower() or "icpc"
    if export_type not in {"icpc", "native"}:
        return json_error_response("unsupported package type", status_code=400)
    try:
        export_job_id = f"exp-api-{new_run_id()}"
        started = start_export_job(
            runtime(),
            identity.problem_slug,
            identity.username,
            actor_user_id=int(identity.user_id),
            problem_id=int(identity.problem_id),
            requested_export_type=export_type,
            export_job_id=export_job_id,
        )
        if not started:
            return json_error_response("export already running for this source", status_code=409)
        return _json_body({"job_id": export_job_id, "status": "queued"})
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)
    except RuntimeError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_export_status(request: Request, job_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    job = runtime().export_service.export_job(
        int(identity.problem_id),
        job_id,
    )
    if job is None or not runtime().access_query.package_job_context(
        actor_user_id=int(identity.user_id),
        problem_id=int(identity.problem_id),
        job_actor_user_id=int(job["actor_user_id"]),
        status=str(job["status"]),
    )["can_view"]:
        return json_error_response("export not found", status_code=404)
    status = str(job.get("status") or "")
    payload: dict[str, object] = {
        "job_id": job_id,
        "status": status,
        "created_at": str(job.get("created_at") or ""),
        "started_at": str(job.get("started_at") or ""),
        "finished_at": str(job.get("finished_at") or ""),
        "export_type": str(job.get("export_type") or ""),
        "source_commit": str(job.get("source_commit") or ""),
        "error": str(job.get("error") or ""),
    }
    if status == "succeeded" and str(job.get("export_id") or "") and str(job.get("filename") or ""):
        payload["download_path"] = f"/agent/v1/export/{job_id}/download"
        payload["filename"] = str(job.get("filename") or "")
    return _json_body(payload)


async def agent_export_download(request: Request, job_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    job = runtime().export_service.export_job(
        int(identity.problem_id),
        job_id,
    )
    if job is None:
        return json_error_response("export not ready", status_code=404)
    access = runtime().access_query.package_job_context(
        actor_user_id=int(identity.user_id),
        problem_id=int(identity.problem_id),
        job_actor_user_id=int(job["actor_user_id"]),
        status=str(job["status"]),
    )
    if not access["can_download"]:
        return json_error_response("export not ready", status_code=404)
    artifact_id = str(job.get("export_id") or "")
    filename = str(job.get("filename") or "")
    if not artifact_id or not filename:
        return json_error_response("export not ready", status_code=404)
    archive_path = runtime().export_service.export_archive_path(
        int(identity.problem_id),
        artifact_id,
        filename,
    )
    if archive_path is None:
        return json_error_response("export not ready", status_code=404)
    return Response(
        content=archive_path.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{Path(filename).name}"'},
    )


async def agent_workspace_files(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    workspace = Path(str(_agent_problem_ctx(identity)["workspace"]["path"])).resolve()
    rel = str(request.query_params.get("path") or "").strip()
    try:
        listed = runtime().workspace_file_service.list_entries(
            workspace,
            rel,
            limit=runtime().config_values.WORKSPACE_FILE_LIST_LIMIT,
            require_allowed_root=True,
        )
        items = [{"path": item.path, "is_dir": item.is_dir, "is_file": item.is_file} for item in listed.entries]
        return _json_body({"base_path": listed.base_path, "entries": items, "truncated": listed.truncated})
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)
    except HTTPException as exc:
        return json_error_response(str(exc.detail), status_code=exc.status_code)


async def agent_workspace_status(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    return _json_body(
        {
            "problem": identity.problem_slug,
            "user": identity.username,
            "workspace_id": int(ctx["workspace"]["id"]),
            "head_commit": str(ctx["workspace"].get("head_commit") or ""),
            "dirty": bool(ctx["workspace"].get("dirty")),
            "git": runtime().git_service.status(workspace),
        }
    )


def _workspace_zip_filename(problem_slug: str, suffix: str) -> str:
    token = str(problem_slug or "problem").strip().replace("/", "-")
    token = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in token).strip("-.")
    return f"{token or 'problem'}-{suffix}.zip"


async def agent_workspace_snapshot(request: Request):
    request_runtime = runtime()
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    try:
        result = request_runtime.workspace_mutation_service.read_locked(
            workspace,
            lambda: request_runtime.workspace_archive_service.build_snapshot_zip(
                workspace
            ),
        )
        status = result.status
        payload = result.value
        head_commit = str(status.get("head_commit") or "")
        dirty = bool(status.get("dirty"))
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{_workspace_zip_filename(identity.problem_slug, "snapshot")}"',
                "X-Problem": identity.problem_slug,
                "X-Head-Commit": head_commit,
                "X-Workspace-Dirty": "true" if dirty else "false",
            },
        )
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_workspace_compare(request: Request, archive: UploadFile = File(...)):
    request_runtime = runtime()
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    try:
        snapshot = request_runtime.config_values.snapshot()
        async with spool_upload(
            archive,
            root=request_runtime.storage_layout.archive_upload_root,
            max_bytes=int(snapshot["UPLOAD_MAX_BYTES"]),
            label="workspace archive",
        ) as archive_path:
            with ArchiveView(
                archive_path,
                problem_archive_policy(
                    int(snapshot["PROBLEM_ZIP_MAX_EXPANDED_BYTES"])
                ),
            ) as package:
                result = request_runtime.workspace_mutation_service.read_locked(
                    workspace,
                    lambda: request_runtime.workspace_archive_service.compare_zip(
                        workspace, package
                    ),
                )
        status = result.status
        diff = result.value
        payload = {
            "problem": identity.problem_slug,
            "head_commit": str(status.get("head_commit") or ""),
            "dirty": bool(status.get("dirty")),
            **diff.as_payload(),
        }
        return _json_body(payload)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_workspace_apply(
    request: Request,
    archive: UploadFile = File(...),
    base_head_commit: str = Form(""),
):
    request_runtime = runtime()
    identity = require_agent_token(request, min_scope="workspace")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    expected_head = str(base_head_commit or "").strip()
    try:
        snapshot = request_runtime.config_values.snapshot()
        async with spool_upload(
            archive,
            root=request_runtime.storage_layout.archive_upload_root,
            max_bytes=int(snapshot["UPLOAD_MAX_BYTES"]),
            label="workspace archive",
        ) as archive_path:
            with ArchiveView(
                archive_path,
                problem_archive_policy(
                    int(snapshot["PROBLEM_ZIP_MAX_EXPANDED_BYTES"])
                ),
            ) as package:
                def apply_archive():
                    status_before = request_runtime.workspace_service.read_workspace_status(
                        workspace
                    )
                    current_head = str(status_before.get("head_commit") or "")
                    if expected_head and expected_head != current_head:
                        raise WorkspaceMutationConflict("workspace head changed")
                    return request_runtime.workspace_archive_service.apply_zip(
                        workspace, package
                    )

                result = request_runtime.workspace_mutation_service.write_locked(
                    workspace, apply_archive
                )
        diff = result.value
        status_after = result.status
        payload = {
            "problem": identity.problem_slug,
            "head_commit": str(status_after.get("head_commit") or ""),
            "dirty": bool(status_after.get("dirty")),
            "applied": True,
            **diff.as_payload(),
        }
        return _json_body(payload)
    except WorkspaceMutationConflict as exc:
        return json_error_response(str(exc), status_code=409)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_workspace_file(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    workspace = Path(str(_agent_problem_ctx(identity)["workspace"]["path"])).resolve()
    rel = str(request.query_params.get("path") or "").strip()
    try:
        file_payload = runtime().workspace_file_service.file_payload(workspace, rel, require_allowed_root=True)
        payload: dict[str, object] = {
            "path": file_payload.path,
            "is_dir": file_payload.is_dir,
        }
        if not file_payload.is_dir:
            payload.update(
                {
                    "size_bytes": file_payload.size_bytes,
                    "media_type": file_payload.media_type,
                    "encoding": file_payload.encoding,
                    "content": file_payload.content,
                }
            )
        return _json_body(payload)
    except FileNotFoundError as exc:
        return json_error_response(str(exc), status_code=404)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)
    except HTTPException as exc:
        return json_error_response(str(exc.detail), status_code=exc.status_code)


async def agent_workspace_upload(request: Request, file: UploadFile = File(...)):
    identity = require_agent_token(request, min_scope="workspace")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    form = await request.form()
    rel = str(form.get("path") or "").strip()
    if not rel:
        return json_error_response("path is required", status_code=400)
    try:
        normalized, total_bytes = await runtime().workspace_file_service.upload_file(workspace, rel, file, require_allowed_root=True)
        return _json_body({"ok": True, "path": normalized, "bytes": total_bytes})
    except HTTPException as exc:
        return json_error_response(str(exc.detail), status_code=exc.status_code)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_workspace_delete(request: Request, path: str):
    identity = require_agent_token(request, min_scope="workspace")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    try:
        normalized = runtime().workspace_file_service.delete_path(workspace, path, require_allowed_root=True)
        return _json_body({"ok": True, "path": normalized})
    except HTTPException as exc:
        return json_error_response(str(exc.detail), status_code=exc.status_code)
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_commit(request: Request):
    identity = require_agent_token(request, min_scope="commit")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    payload = await _read_json(request)
    message = str(payload.get("message") or "").strip()
    if not message:
        return json_error_response("message is required", status_code=400)
    commit_created = False
    commit_head = ""
    try:
        with runtime().workspace_service.workspace_lock(workspace):
            try:
                commit_head = runtime().git_service.commit(workspace, message, identity.username, f"{identity.username}@polygonlike.local")
                commit_created = True
            except Exception as commit_exc:
                commit_err = str(commit_exc or "")
                commit_err_lower = commit_err.lower()
                if "nothing to commit" not in commit_err_lower and "no changes added to commit" not in commit_err_lower:
                    raise
            try:
                runtime().git_service.push(workspace, "main")
            except Exception as push_exc:
                if commit_created:
                    runtime().git_service.rollback_last_commit(workspace, expected_head=commit_head)
                raise push_exc
        return _json_body({"status": "ok", "head": commit_head})
    except Exception as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_commit_status(request: Request, ref: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    local_head = run_git(["git", "-C", str(workspace), "rev-parse", "HEAD"]).stdout.strip()
    remote_head = run_git(["git", "-C", str(workspace), "rev-parse", "refs/remotes/origin/main"]).stdout.strip()
    safe_ref = str(ref or "").strip()
    status = "unknown"
    if safe_ref and safe_ref == local_head == remote_head:
        status = "published"
    elif safe_ref and safe_ref == local_head:
        status = "local"
    elif safe_ref:
        status = "missing"
    return _json_body({"ref": safe_ref, "status": status, "head": local_head, "remote_head": remote_head})
