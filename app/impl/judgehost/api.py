from __future__ import annotations

import base64
import json
import logging
from functools import partial
from urllib.parse import parse_qsl

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from app.impl.runtime.config import config


JudgehostPayload = dict[str, str | bytes]


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    token = request.headers.get("x-judgehost-token") or ""
    return token.strip()


def _extract_basic_credentials(request: Request) -> tuple[str, str]:
    auth_header = request.headers.get("authorization") or ""
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
    return (user, password)


def _extract_hostname(payload: JudgehostPayload, request: Request) -> str:
    hostname = (payload.get("hostname") or "").strip()
    if hostname:
        return hostname
    peer = request.client.host if request.client is not None else ""
    return peer or "judgehost"


def _hostname_from_payload(payload: JudgehostPayload, request: Request, *, required: bool = False) -> str:
    hostname = _extract_hostname(payload, request)
    if required and not hostname:
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
_FORM_RAW_BINARY_KEYS = {
    "output_run",
    "output_error",
    "output_system",
    "output_diff",
    "metadata",
    "compare_metadata",
    "team_message",
}
_JUDGEHOST_FORM_PART_LIMIT_BYTES = 16 * 1024 * 1024
_JUDGEHOST_FORM_PART_LIMIT_HEADROOM_BYTES = 1024 * 1024
_logger = logging.getLogger(__name__)
_diag_logger = logging.getLogger("uvicorn.error")


def _judgehost_form_part_limit_bytes() -> int:
    service = getattr(config, "judgehost_task_service", None)
    constants = getattr(service, "_constants", None)

    def _read_kb(name: str, fallback: int = 0) -> int:
        if constants is None:
            return fallback
        try:
            return max(0, int(getattr(constants, name, fallback) or fallback))
        except Exception:
            return fallback

    output_kb = max(
        _read_kb("RUN_EXEC_OUTPUT_KB"),
        _read_kb("VERIFICATION_EXEC_OUTPUT_KB"),
        _read_kb("TOOLCHAIN_COMPILE_OUTPUT_KB"),
    )
    if output_kb <= 0:
        return _JUDGEHOST_FORM_PART_LIMIT_BYTES
    return max(
        _JUDGEHOST_FORM_PART_LIMIT_BYTES,
        int(output_kb * 1024) + _JUDGEHOST_FORM_PART_LIMIT_HEADROOM_BYTES,
    )


async def _coerce_form_value(key: str, value: str | UploadFile) -> str | bytes:
    if isinstance(value, UploadFile):
        try:
            raw = await value.read()  # type: ignore[union-attr]
        finally:
            try:
                await value.close()  # type: ignore[union-attr]
            except Exception:
                pass
        if key in _FORM_BINARY_KEYS:
            if not raw:
                return b"" if key in _FORM_RAW_BINARY_KEYS else ""
            if key in _FORM_RAW_BINARY_KEYS:
                return raw
            return base64.b64encode(raw).decode("ascii")
        return raw.decode("utf-8", errors="replace")
    return value


async def _request_payload(request: Request) -> JudgehostPayload:
    def _merge_text_field(out: JudgehostPayload, key: str, text: str) -> None:
        if key not in out:
            out[key] = text
            return
        prev = out[key]
        if not prev.strip():
            out[key] = text
            return
        if (not text.strip()) or (text == prev):
            return
        out[key] = f"{prev}\n{text}"

    content_type = (request.headers.get("content-type") or "").strip().lower()

    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        return dict(payload)
    if ("application/x-www-form-urlencoded" in content_type) or ("multipart/form-data" in content_type):
        if "multipart/form-data" in content_type:
            # Judgehost payloads may include >1MB parts (program output/logs).
            part_limit_bytes = _judgehost_form_part_limit_bytes()
            MultiPartParser.max_part_size = max(
                int(getattr(MultiPartParser, "max_part_size", 0) or 0),
                part_limit_bytes,
            )
        try:
            form = await request.form(max_files=4096, max_fields=4096)
        except Exception as exc:
            _logger.warning("judgehost multipart parse failed content_type=%s: %s", content_type, exc)
            return {}
        out: JudgehostPayload = {}
        items = form.multi_items()
        for key, value in items:
            text = await _coerce_form_value(key, value)
            if key in _FORM_BINARY_KEYS:
                prev = out.get(key)
                if key not in out:
                    out[key] = text
                    continue
                if not prev:
                    out[key] = text
                continue
            _merge_text_field(out, key, text)
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
    try:
        return dict(parsed_json)
    except Exception:
        pass
    text = body.decode("utf-8", errors="replace")
    pairs = parse_qsl(text, keep_blank_values=True)
    if not pairs:
        return {}
    out: JudgehostPayload = {}
    for key, value in pairs:
        _merge_text_field(out, key, value)
    return out


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
    if raw is None:
        return None
    text = str(raw).strip()
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
    hostname = (payload.get("hostname") or "").strip()
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
    if tasks:
        _diag_logger.warning(
            "judgehost.fetch_work host=%s tasks=%s",
            hostname,
            [
                {
                    "type": task["type"],
                    "jobid": task["jobid"],
                    "submitid": task["submitid"],
                    "judgetaskid": task["judgetaskid"],
                    "testcase_id": task["testcase_id"],
                }
                for task in tasks
            ],
        )
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
    token = file_type.strip().lower()
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
        compiler=(payload.get("compiler") or "").strip(),
        runner=(payload.get("runner") or "").strip(),
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
    description = (payload.get("description") or "").strip()
    judgetask_id = None
    for key in ("judgetaskid", "run_id"):
        judgetask_id = _int_or_none(payload.get(key))
        if judgetask_id is not None:
            break
    result = await _run_service_call(
        service.domjudge_internal_error,
        description=description,
        judgetask_id=judgetask_id,
        payload=payload,
    )
    return JSONResponse(int(result))


async def domjudge_add_debug_info(request: Request, hostname: str, judgetask_id: int):
    service = _require_judgehost_auth(request)
    payload = await _request_payload(request)
    await _run_service_call(
        service.domjudge_add_debug_info,
        hostname=hostname,
        judgetask_id=judgetask_id,
        payload=payload,
    )
    return JSONResponse({})

