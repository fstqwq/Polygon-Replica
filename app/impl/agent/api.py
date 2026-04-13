from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import cast

from fastapi import File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.impl.agent.shared import require_agent_token, workspace_context_for_identity
from app.impl.auth.shared import json_error_response
from app.impl.runtime.config import config
from app.impl.workspace.context_job import start_export_job, start_verification_job
from app.impl.workspace.context_job_helper import allocate_run_id, allocate_verification_id
from app.impl.workspace.context_operation import audit, run_solution_options_context, workspace_rel_file_exists
from app.main_util import normalize_workspace_rel_path, safe_workspace_path, write_upload_file_limited
from app.service.platform.git_process import run_git
from app.service.problem.solution_metadata import normalize_expected_behavior


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


def _build_verification_targets(workspace: Path) -> tuple[list[dict[str, object]], str]:
    solution_options, accepted_source, _ = run_solution_options_context(workspace)
    safe_accepted_source = str(accepted_source or "")
    if not safe_accepted_source:
        raise ValueError("main correct solution is required")
    if not workspace_rel_file_exists(workspace, safe_accepted_source):
        raise ValueError("main correct solution source does not exist")
    targets: list[dict[str, object]] = []
    for row in solution_options:
        source_path = str(row.get("path") or "")
        if not source_path:
            continue
        expected_behavior = normalize_expected_behavior(str(row.get("expected_behavior") or "unknown"))
        if source_path == safe_accepted_source or bool(row.get("is_accepted")):
            expected_behavior = "accepted"
        targets.append({"path": source_path, "expected_behavior": expected_behavior})
    if not targets:
        raise ValueError("at least one solution source is required")
    if not any(str(item.get("expected_behavior") or "") == "accepted" for item in targets):
        raise ValueError("accepted solution source is required")
    targets.sort(key=lambda item: (0 if item["expected_behavior"] == "accepted" else 1, str(item["path"])))
    for target in targets:
        target["run_id"] = allocate_run_id()
    return targets, safe_accepted_source


async def agent_register(request: Request, code: str):
    payload = await _read_json(request)
    try:
        result = config.agent_service.register_agent(
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
        result = config.agent_service.request_problem_access(
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
        result = config.agent_service.poll_access_request(
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
        result = config.agent_service.session_status(
            agent_session_id=session_id,
            identity_hash=identity_hash,
        )
        return _json_body(result)
    except PermissionError as exc:
        return json_error_response(str(exc), status_code=401)


async def agent_verification_start(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    workspace = Path(str(ctx["workspace"]["path"])).resolve()
    verification_id = allocate_verification_id()
    try:
        targets, accepted_source = _build_verification_targets(workspace)
        details: dict[str, object] = {
            "status": "running",
            "steps": ["gen", "val", "run", "check"],
            "workspace_head": str(ctx["workspace"].get("head_commit") or ""),
            "workspace_dirty": bool(ctx["workspace"].get("dirty")),
            "verification_id": verification_id,
            "artifact_verification_id": verification_id,
            "verification_source": "agent.v1.verification.start",
            "task_graph": True,
            "error": "",
            "submission_paths": [str(item["path"]) for item in targets],
            "solution_count": len(targets),
            "accepted_source": accepted_source,
        }
        started = start_verification_job(
            identity.problem_slug,
            identity.username,
            actor_user_id=int(identity.user_id),
            problem_id=int(identity.problem_id),
            workspace_id=int(ctx["workspace"]["id"]),
            workspace_head=str(ctx["workspace"].get("head_commit") or ""),
            workspace_dirty=bool(ctx["workspace"].get("dirty")),
            targets=targets,
            verification_id=verification_id,
            initial_details=details,
            workspace_path=workspace,
        )
        if not started:
            return json_error_response("verification already running", status_code=409)
        audit(int(identity.user_id), int(identity.problem_id), "agent.verification.start", {"verification_id": verification_id})
        return _json_body({"verification_id": verification_id, "status": "queued"})
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_verification_status(request: Request, verification_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    detail = config.verification_service.workspace_verification_detail(
        int(identity.problem_id),
        int(ctx["workspace"]["id"]),
        verification_id,
    )
    if detail is None:
        return json_error_response("verification not found", status_code=404)
    runtime_summary = config.verification_service.verification_runtime_summary(verification_id)
    return _json_body(
        {
            "verification_id": verification_id,
            "status": str(detail["status"] or ""),
            "runtime_summary": runtime_summary,
        }
    )


async def agent_verification_detail(request: Request, verification_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    record = config.verification_service.verification_record(verification_id)
    detail = config.verification_service.workspace_verification_detail(
        int(identity.problem_id),
        int(ctx["workspace"]["id"]),
        verification_id,
    )
    if record is None or detail is None:
        return json_error_response("verification not found", status_code=404)
    runtime_summary = config.verification_service.verification_runtime_summary(verification_id)
    return _json_body(
        {
            "verification": record,
            "detail": detail["details"],
            "runtime_summary": runtime_summary,
        }
    )


async def agent_verification_detail_text(request: Request, verification_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    record = config.verification_service.verification_record(verification_id)
    detail = config.verification_service.workspace_verification_detail(
        int(identity.problem_id),
        int(ctx["workspace"]["id"]),
        verification_id,
    )
    if record is None or detail is None:
        return PlainTextResponse("verification not found", status_code=404)
    runtime_summary = config.verification_service.verification_runtime_summary(verification_id)
    payload = {
        "verification": record,
        "detail": detail["details"],
        "runtime_summary": runtime_summary,
    }
    return PlainTextResponse(json.dumps(payload, ensure_ascii=False, indent=2), media_type="text/plain; charset=utf-8")


def _find_export_event(*, problem_id: int, actor_user_id: int, export_task_id: str) -> dict[str, object] | None:
    rows = config.db.fetch_all(
        """
        SELECT created_at,details_json
        FROM audit_log
        WHERE problem_id=? AND actor_user_id=? AND action='export.create'
        ORDER BY created_at DESC
        LIMIT 200
        """,
        [int(problem_id), int(actor_user_id)],
    )
    for row in rows:
        raw = str(row["details_json"] or "")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("export_task_id") or "") != str(export_task_id or ""):
            continue
        payload["created_at"] = str(row["created_at"] or "")
        return payload
    return None


def _find_export_record_for_event(*, problem_id: int, workspace_id: int, event: dict[str, object]) -> dict[str, object] | None:
    filename = str(event.get("filename") or "")
    export_type = str(event.get("export_type") or "")
    source_commit = str(event.get("source_commit") or "")
    for row in config.export_service.workspace_exports(int(problem_id), int(workspace_id), limit=100):
        if str(row.get("filename") or "") != filename:
            continue
        if str(row.get("export_type") or "") != export_type:
            continue
        if str(row.get("source_commit") or "") != source_commit:
            continue
        return dict(row)
    return None


async def agent_export_start(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    payload = await _read_json(request)
    export_type = str(payload.get("export_type") or "icpc").strip().lower() or "icpc"
    verification_id = str(payload.get("verification_id") or "")
    workspace_head = str(ctx["workspace"].get("head_commit") or "")
    source_commit = "" if export_type == "native" else workspace_head
    export_task_id = f"exp-api-{Path(verification_id).name}" if verification_id else f"exp-api-{allocate_run_id()}"
    initial_details: dict[str, object] = {
        "status": "running",
        "verification_id": verification_id,
        "export_type": export_type,
        "source_commit": source_commit,
        "filename": "",
        "error": "",
        "export_task_id": export_task_id,
    }
    try:
        started = start_export_job(
            identity.problem_slug,
            identity.username,
            actor_user_id=int(identity.user_id),
            problem_id=int(identity.problem_id),
            workspace_id=int(ctx["workspace"]["id"]),
            source_commit=source_commit,
            requested_verification_id=verification_id,
            requested_export_type=export_type,
            export_task_id=export_task_id,
            initial_details=initial_details,
        )
        if not started:
            return json_error_response("export already running for this source", status_code=409)
        audit(int(identity.user_id), int(identity.problem_id), "agent.export.start", {"export_task_id": export_task_id, "export_type": export_type})
        return _json_body({"export_id": export_task_id, "status": "queued"})
    except ValueError as exc:
        return json_error_response(str(exc), status_code=400)
    except RuntimeError as exc:
        return json_error_response(str(exc), status_code=400)


async def agent_export_status(request: Request, export_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    event = _find_export_event(problem_id=int(identity.problem_id), actor_user_id=int(identity.user_id), export_task_id=export_id)
    if event is None:
        return json_error_response("export not found", status_code=404)
    status = str(event.get("status") or "")
    payload: dict[str, object] = {
        "export_id": export_id,
        "status": status,
        "created_at": str(event.get("created_at") or ""),
        "export_type": str(event.get("export_type") or ""),
        "source_commit": str(event.get("source_commit") or ""),
        "error": str(event.get("error") or ""),
    }
    if status == "ok":
        record = _find_export_record_for_event(problem_id=int(identity.problem_id), workspace_id=int(ctx["workspace"]["id"]), event=event)
        if record is not None:
            payload["download_path"] = f"/agent/v1/export/{export_id}/download"
            payload["filename"] = str(record.get("filename") or "")
    return _json_body(payload)


async def agent_export_download(request: Request, export_id: str):
    identity = require_agent_token(request, min_scope="readonly")
    ctx = _agent_problem_ctx(identity)
    event = _find_export_event(problem_id=int(identity.problem_id), actor_user_id=int(identity.user_id), export_task_id=export_id)
    if event is None or str(event.get("status") or "") != "ok":
        return json_error_response("export not ready", status_code=404)
    record = _find_export_record_for_event(problem_id=int(identity.problem_id), workspace_id=int(ctx["workspace"]["id"]), event=event)
    if record is None:
        return json_error_response("export not ready", status_code=404)
    archive_path = config.export_service.export_archive_path(
        int(identity.problem_id),
        int(ctx["workspace"]["id"]),
        str(record["id"]),
        identity.problem_slug,
        str(record["filename"]),
    )
    if archive_path is None:
        return json_error_response("export not ready", status_code=404)
    return Response(
        content=archive_path.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{Path(str(record["filename"]).strip()).name}"'},
    )


async def agent_workspace_files(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    workspace = Path(str(_agent_problem_ctx(identity)["workspace"]["path"])).resolve()
    rel = str(request.query_params.get("path") or "").strip()
    try:
        normalized = normalize_workspace_rel_path(rel)
        entries, truncated = config.git_service.list_files_capped(workspace, normalized or ".", limit=config.constants.WORKSPACE_FILE_LIST_LIMIT)
        items: list[dict[str, object]] = []
        for entry in entries:
            target = safe_workspace_path(workspace, entry)
            items.append({
                "path": entry,
                "is_dir": bool(target.exists() and target.is_dir()),
                "is_file": bool(target.exists() and target.is_file()),
            })
        return _json_body({"base_path": normalized, "entries": items, "truncated": truncated})
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
            "git": config.git_service.status(workspace),
        }
    )


async def agent_workspace_file(request: Request):
    identity = require_agent_token(request, min_scope="readonly")
    workspace = Path(str(_agent_problem_ctx(identity)["workspace"]["path"])).resolve()
    rel = str(request.query_params.get("path") or "").strip()
    try:
        normalized = normalize_workspace_rel_path(rel)
        if not normalized:
            return json_error_response("path is required", status_code=400)
        target = safe_workspace_path(workspace, normalized)
        if not target.exists() or target.is_symlink():
            return json_error_response("file not found", status_code=404)
        if target.is_dir():
            return _json_body({"path": normalized, "is_dir": True})
        raw = target.read_bytes()
        media_type = "application/octet-stream"
        content_text = ""
        content_b64 = ""
        try:
            content_text = raw.decode("utf-8")
            media_type = "text/plain; charset=utf-8"
        except Exception:
            content_b64 = base64.b64encode(raw).decode("ascii")
        payload: dict[str, object] = {
            "path": normalized,
            "is_dir": False,
            "size_bytes": len(raw),
            "media_type": media_type,
        }
        if content_b64:
            payload["encoding"] = "base64"
            payload["content"] = content_b64
        else:
            payload["encoding"] = "utf-8"
            payload["content"] = content_text
        return _json_body(payload)
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
        normalized = normalize_workspace_rel_path(rel)
        if not normalized:
            return json_error_response("path is required", status_code=400)
        total_bytes = 0
        with config.workspace_service.workspace_lock(workspace):
            target = safe_workspace_path(workspace, normalized)
            if target.exists() and target.is_dir():
                return json_error_response("upload target must be a file path", status_code=400)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                total_bytes = await write_upload_file_limited(file, handle)
        audit(int(identity.user_id), int(identity.problem_id), "agent.workspace.upload", {"path": normalized, "bytes": total_bytes})
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
        normalized = normalize_workspace_rel_path(path)
        if not normalized:
            return json_error_response("path is required", status_code=400)
        with config.workspace_service.workspace_lock(workspace):
            config.git_service.delete_path(workspace, normalized)
        audit(int(identity.user_id), int(identity.problem_id), "agent.workspace.delete", {"path": normalized})
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
        with config.workspace_service.workspace_lock(workspace):
            try:
                commit_head = config.git_service.commit(workspace, message, identity.username, f"{identity.username}@polygonlike.local")
                commit_created = True
            except Exception as commit_exc:
                commit_err = str(commit_exc or "")
                commit_err_lower = commit_err.lower()
                if "nothing to commit" not in commit_err_lower and "no changes added to commit" not in commit_err_lower:
                    raise
            try:
                config.git_service.push(workspace, "main")
            except Exception as push_exc:
                if commit_created:
                    config.git_service.rollback_last_commit(workspace, expected_head=commit_head)
                raise push_exc
        audit(int(identity.user_id), int(identity.problem_id), "agent.commit", {"message": message, "head": commit_head})
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
