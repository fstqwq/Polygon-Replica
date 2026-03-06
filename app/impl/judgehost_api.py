from __future__ import annotations

import base64
import json
from functools import partial
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from app.impl.config import config


def _json_payload_or_empty(payload: object) -> dict[str, object]:
    if isinstance(payload, dict):
        return dict(payload)
    return {}


def _extract_bearer_token(request: Request) -> str:
    auth_header = str(request.headers.get("authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return str(request.headers.get("x-judgehost-token") or "").strip()


def _extract_basic_credentials(request: Request) -> tuple[str, str]:
    auth_header = str(request.headers.get("authorization") or "").strip()
    if not auth_header.lower().startswith("basic "):
        return ("", "")
    raw = auth_header[6:].strip()
    if not raw:
        return ("", "")
    try:
        decoded = base64.b64decode(raw, validate=False).decode("utf-8", errors="replace")
    except Exception:
        return ("", "")
    if ":" not in decoded:
        return ("", "")
    user, password = decoded.split(":", 1)
    return (str(user or "").strip(), str(password or "").strip())


def _extract_hostname(payload: dict[str, object], request: Request) -> str:
    raw = str(payload.get("hostname") or "").strip()
    if raw:
        return raw
    peer = request.client.host if request.client is not None else ""
    return str(peer or "judgehost")


def _hostname_from_payload(payload: dict[str, object], request: Request, *, required: bool = False) -> str:
    hostname = _extract_hostname(payload, request)
    if required and not str(hostname or "").strip():
        raise HTTPException(status_code=400, detail="hostname is required")
    return hostname


_FORM_BINARY_KEYS = {
    "output_run",
    "output_error",
    "output_system",
    "output_diff",
    "metadata",
    "compare_metadata",
    "team_message",
    "output_compile",
    "compile_metadata",
}
_JUDGEHOST_FORM_PART_LIMIT_BYTES = 16 * 1024 * 1024


async def _coerce_form_value(key: str, value: object) -> str:
    token = str(key or "").strip()
    if isinstance(value, UploadFile):
        try:
            raw = await value.read()
        finally:
            try:
                await value.close()
            except Exception:
                pass
        if token in _FORM_BINARY_KEYS:
            if not raw:
                return ""
            return base64.b64encode(raw).decode("ascii")
        return raw.decode("utf-8", errors="replace")
    return str(value)


async def _request_payload(request: Request) -> dict[str, object]:
    content_type = str(request.headers.get("content-type") or "").strip().lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return _json_payload_or_empty(payload)
    if ("application/x-www-form-urlencoded" in content_type) or ("multipart/form-data" in content_type):
        if "multipart/form-data" in content_type:
            # Judgehost payloads may include >1MB parts (program output/logs).
            MultiPartParser.max_part_size = max(
                int(getattr(MultiPartParser, "max_part_size", 0) or 0),
                _JUDGEHOST_FORM_PART_LIMIT_BYTES,
            )
            MultiPartParser.max_file_size = max(
                int(getattr(MultiPartParser, "max_file_size", 0) or 0),
                _JUDGEHOST_FORM_PART_LIMIT_BYTES,
            )
        try:
            form = await request.form(max_files=4096, max_fields=4096)
        except Exception:
            form = {}
        out: dict[str, object] = {}
        if hasattr(form, "multi_items"):
            items = form.multi_items()
        elif isinstance(form, dict):
            items = form.items()
        else:
            items = []
        for key, value in items:
            safe_key = str(key)
            text = await _coerce_form_value(safe_key, value)
            if (safe_key not in out) or (not str(out[safe_key]).strip()):
                out[safe_key] = text
        return out
    try:
        body = await request.body()
    except Exception:
        body = b""
    if not body:
        return {}
    try:
        parsed_json = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        parsed_json = None
    if isinstance(parsed_json, dict):
        return dict(parsed_json)
    text = body.decode("utf-8", errors="replace")
    pairs = parse_qsl(text, keep_blank_values=True)
    if not pairs:
        return {}
    return {str(k): str(v) for k, v in pairs}


def _require_judgehost_auth(request: Request):
    service = config.judgehost_task_service
    if not service.enabled():
        raise HTTPException(status_code=404, detail="judgehost API is disabled")
    if not service.auth_token_configured():
        raise HTTPException(status_code=503, detail="judgehost API token is not configured")
    token = _extract_bearer_token(request)
    if token and service.check_api_token(token):
        return service
    basic_user, basic_pass = _extract_basic_credentials(request)
    if basic_user and basic_pass and service.check_api_basic(basic_user, basic_pass):
        return service
    raise HTTPException(status_code=401, detail="invalid judgehost credentials")




def _int_or_none(raw: object) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


async def _run_service_call(fn, /, *args, **kwargs):
    if kwargs:
        return await run_in_threadpool(partial(fn, *args, **kwargs))
    return await run_in_threadpool(fn, *args)


async def domjudge_config(request: Request):
    service = _require_judgehost_auth(request)
    return JSONResponse(await _run_service_call(service.domjudge_config))


async def domjudge_languages(request: Request):
    service = _require_judgehost_auth(request)
    return JSONResponse(await _run_service_call(service.domjudge_languages))


async def domjudge_judgehosts_get(request: Request):
    service = _require_judgehost_auth(request)
    return JSONResponse(await _run_service_call(service.domjudge_list_hosts))


async def domjudge_judgehosts_post(request: Request):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    hostname = str(payload.get("hostname") or "").strip()
    if not hostname:
        raise HTTPException(status_code=400, detail="hostname is required")
    rows = await _run_service_call(service.domjudge_register_host, hostname)
    return JSONResponse(rows)


async def domjudge_fetch_work(request: Request):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    hostname = _hostname_from_payload(payload, request)
    max_batchsize = _int_or_none(payload.get("max_batchsize"))
    try:
        tasks = await _run_service_call(service.domjudge_fetch_work, hostname, max_batchsize=max_batchsize)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(tasks)


async def domjudge_get_files_source(request: Request, contest_id: str, item_id: str):
    service = _require_judgehost_auth(request)
    try:
        rows = await _run_service_call(service.domjudge_get_source_files, item_id, contest_id=contest_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(rows)


async def domjudge_get_files_source_submit(request: Request, item_id: str):
    service = _require_judgehost_auth(request)
    try:
        rows = await _run_service_call(service.domjudge_get_source_files, item_id, contest_id=None)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(rows)


async def domjudge_get_files_by_type(request: Request, file_type: str, item_id: str):
    service = _require_judgehost_auth(request)
    token = str(file_type or "").strip().lower()
    try:
        if token == "testcase":
            test_id = _int_or_none(item_id)
            if test_id is None:
                raise RuntimeError("invalid testcase id")
            rows = await _run_service_call(service.domjudge_get_testcase_files, test_id)
        elif token in {"compile", "run", "compare"}:
            rows = await _run_service_call(service.domjudge_get_executable_files, token, item_id)
        else:
            raise RuntimeError("unknown file type")
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(rows)


async def domjudge_get_version_commands(request: Request, judgetask_id: int):
    service = _require_judgehost_auth(request)
    return JSONResponse(await _run_service_call(service.domjudge_get_version_commands, judgetask_id))


async def domjudge_check_versions(request: Request, judgetask_id: int):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    hostname = _hostname_from_payload(payload, request)
    result = await _run_service_call(
        service.domjudge_check_versions,
        judgetask_id,
        hostname=hostname,
        compiler=str(payload.get("compiler") or ""),
        runner=str(payload.get("runner") or ""),
    )
    return JSONResponse(result)


async def domjudge_update_judging(request: Request, hostname: str, judgetask_id: int):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    try:
        await _run_service_call(service.domjudge_update_judging, hostname, judgetask_id, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({})


async def domjudge_add_judging_run(request: Request, hostname: str, judgetask_id: int):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    try:
        result = await _run_service_call(service.domjudge_add_judging_run, hostname, judgetask_id, payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(int(result))


async def domjudge_internal_error(request: Request):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    description = str(payload.get("description") or "").strip()
    judgetask_id = _int_or_none(payload.get("judgetaskid"))
    if judgetask_id is None:
        judgetask_id = _int_or_none(payload.get("judgetask_id"))
    result = await _run_service_call(
        service.domjudge_internal_error,
        description=description,
        judgetask_id=judgetask_id,
    )
    return JSONResponse(int(result))


async def domjudge_add_debug_info(request: Request, hostname: str, judgetask_id: int):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    if not isinstance(payload, dict):
        payload = {}
    await _run_service_call(
        service.domjudge_add_debug_info,
        hostname=hostname,
        judgetask_id=judgetask_id,
        payload=payload,
    )
    return JSONResponse({})

