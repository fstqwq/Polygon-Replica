import base64
import json
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import app.main_constant as _K
from app.impl.contest.workspace_scope import (
    problem_template_navigation,
)
from app.impl.runtime.dependency import runtime
from app.service.platform.hashing import hmac_sha256_hex, sha256_hex_bytes


def install_template_filters(templates: Jinja2Templates) -> None:
    """Install the common display filters on one runtime-owned environment."""

    templates.env.filters["local_time"] = _format_local_time
    templates.env.filters["status_label"] = _format_status_label


def _runtime_judgehost_health_profile() -> dict[str, str]:
    try:
        status = runtime().judgehost_task_service.public_status()
    except Exception:
        return {
            "runtime_judgehost_health_summary": "offline",
            "runtime_judgehost_health_tone": "danger",
            "runtime_judgehost_enabled": "0",
            "runtime_judgehost_hosts_online": "0",
            "runtime_judgehost_hosts_total": "0",
        }
    return {
        "runtime_judgehost_health_summary": status["summary"],
        "runtime_judgehost_health_tone": status["tone"],
        "runtime_judgehost_enabled": "1" if status["enabled"] else "0",
        "runtime_judgehost_hosts_online": str(status["hosts_online"]),
        "runtime_judgehost_hosts_total": str(status["hosts_total"]),
    }

def parse_iso_utc(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except Exception:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_local_time(raw: object) -> str:
    if isinstance(raw, datetime):
        try:
            return raw.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw.strftime("%Y-%m-%d %H:%M:%S")
    text = str(raw or "").strip()
    if not text:
        return "-"
    parsed = parse_iso_utc(text)
    if parsed is None:
        return text
    try:
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")


_STATUS_LABEL_MAP: dict[str, str] = {
    "ok": "OK",
    "success": "SUCCESS",
    "succeeded": "SUCCEEDED",
    "failed": "FAILED",
    "error": "ERROR",
    "running": "RUNNING",
    "queued": "QUEUED",
    "pending": "PENDING",
    "stale": "STALE",
    "missing": "MISSING",
    "invalid": "INVALID",
    "none": "NONE",
    "ready": "READY",
    "not_ready": "NOT READY",
    "online": "ONLINE",
    "offline": "OFFLINE",
    "ac": "AC",
    "wa": "WA",
    "pe": "PE",
    "re": "RE",
    "tle": "TLE",
    "mle": "MLE",
    "ce": "CE",
    "se": "SE",
}


def _format_status_label(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return "-"
    normalized = re.sub(r"[\s\-]+", "_", text.lower()).strip("_")
    if not normalized:
        return "-"
    mapped = _STATUS_LABEL_MAP.get(normalized)
    if mapped:
        return mapped
    tokens = [tok for tok in re.split(r"[_\s\-]+", normalized) if tok]
    if not tokens:
        return text
    words: list[str] = []
    for token in tokens:
        if token in _STATUS_LABEL_MAP:
            words.append(_STATUS_LABEL_MAP[token])
        elif len(token) <= 3 and token.isalpha():
            words.append(token.upper())
        else:
            words.append(token.capitalize())
    return " ".join(words)


def _normalize_flash_message(raw: object) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    text = "\n".join(lines).strip()
    if text and text[-1] not in ".!?":
        if re.search(r"[A-Za-z0-9\)]$", text):
            text += "."
    max_length = runtime().config_values.integer("FLASH_MESSAGE_MAX_LEN")
    if len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def _flash_message_level(message: str) -> str:
    text = message.lower()
    if not text:
        return "info"
    if any((token in text for token in {"error", "failed", "invalid", "denied", "rejected"})):
        return "error"
    if any((token in text for token in {"warning", "stale", "already running", "already exists"})):
        return "warning"
    if any((token in text for token in {"saved", "created", "updated", "queued", "running", "ok", "done", "success"})):
        return "success"
    return "info"


def _flash_message_event_id(message: str, *, scope: str = "") -> str:
    normalized = _normalize_flash_message(message)
    if not normalized:
        return ""
    payload = f'{str(scope or "").strip()}|{normalized}'.encode("utf-8")
    return sha256_hex_bytes(payload)[:16]


def _decode_flash_queue(raw_cookie: str) -> list[str]:
    token = str(raw_cookie or "").strip()
    if not token:
        return []
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        raw_queue = json.loads(payload)
    except Exception:
        return []
    if not isinstance(raw_queue, list):
        return []
    queue: list[str] = []
    for item in raw_queue:
        normalized = _normalize_flash_message(item)
        if not normalized:
            continue
        queue.append(normalized)
        if len(queue) >= runtime().config_values.integer("FLASH_QUEUE_MAX_ITEMS"):
            break
    return queue


def _encode_flash_queue(queue: list[str]) -> str:
    safe_items: list[str] = []
    for item in queue:
        normalized = _normalize_flash_message(item)
        if not normalized:
            continue
        safe_items.append(normalized)
        if len(safe_items) >= runtime().config_values.integer(
            "FLASH_QUEUE_MAX_ITEMS"
        ):
            break
    if not safe_items:
        return ""
    payload = json.dumps(safe_items, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def set_flash_cookie(response, queue: list[str]) -> None:
    encoded = _encode_flash_queue(queue)
    if not encoded:
        response.delete_cookie(
            runtime().config_values.text("FLASH_COOKIE_NAME"),
            path="/",
            secure=runtime().config_values.boolean("AUTH_COOKIE_SECURE"),
            httponly=True,
            samesite="lax",
        )
        return
    response.set_cookie(
        runtime().config_values.text("FLASH_COOKIE_NAME"),
        encoded,
        httponly=True,
        samesite="lax",
        secure=runtime().config_values.boolean("AUTH_COOKIE_SECURE"),
        max_age=runtime().config_values.integer("FLASH_COOKIE_MAX_AGE"),
        path="/",
    )


def _sanitize_redirect_target(target: str) -> str:
    url = str(target or "").strip() or "/"
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == "message":
            continue
        kept.append((key, value))
    cleaned = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(kept, doseq=True), parsed.fragment)
    )
    if parsed.scheme or parsed.netloc:
        return cleaned or url
    if not cleaned:
        return url
    return cleaned


def _apply_security_headers(response) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"


def redirect_response(url: str, status_code: int = 303, message: str = "") -> RedirectResponse:
    target = _sanitize_redirect_target(url)
    response = RedirectResponse(target, status_code=status_code)
    safe_message = _normalize_flash_message(message)
    if safe_message:
        set_flash_cookie(response, [safe_message])
    _apply_security_headers(response)
    return response


def json_redirect_response(
    url: str,
    message: str = "",
    *,
    payload: Mapping[str, object] | None = None,
) -> JSONResponse:
    target = _sanitize_redirect_target(url)
    safe_message = _normalize_flash_message(message)
    body: dict[str, object] = {"ok": True, "redirect": target, "message": safe_message}
    if payload is not None:
        body.update(payload)
    response = JSONResponse(body)
    if safe_message:
        set_flash_cookie(response, [safe_message])
    _apply_security_headers(response)
    return response


def json_error_response(
    message: str = "",
    status_code: int = 400,
) -> JSONResponse:
    safe_message = _normalize_flash_message(message) or "request failed"
    body: dict[str, object] = {"ok": False, "error": safe_message}
    response = JSONResponse(body, status_code=status_code)
    _apply_security_headers(response)
    return response


def template_response(request: Request, template_name: str, context: dict | None = None):
    payload = dict(context or {})
    payload.setdefault("ui_brand_name", str(runtime().config_values.UI_BRAND_NAME))
    payload.setdefault("ui_brand_tagline", str(runtime().config_values.UI_BRAND_TAGLINE))
    payload.setdefault("ui_browser_title", str(runtime().config_values.UI_BROWSER_TITLE))
    runtime_judgehost = _runtime_judgehost_health_profile()
    if "runtime_judgehost_health_summary" not in payload:
        payload["runtime_judgehost_health_summary"] = runtime_judgehost.get(
            "runtime_judgehost_health_summary",
            "disabled",
        )
    if "runtime_judgehost_health_tone" not in payload:
        payload["runtime_judgehost_health_tone"] = runtime_judgehost.get(
            "runtime_judgehost_health_tone",
            "danger",
        )
    if "runtime_judgehost_enabled" not in payload:
        payload["runtime_judgehost_enabled"] = runtime_judgehost.get("runtime_judgehost_enabled", "0")
    if "runtime_judgehost_hosts_online" not in payload:
        payload["runtime_judgehost_hosts_online"] = runtime_judgehost.get("runtime_judgehost_hosts_online", "0")
    if "runtime_judgehost_hosts_total" not in payload:
        payload["runtime_judgehost_hosts_total"] = runtime_judgehost.get("runtime_judgehost_hosts_total", "0")
    if "runtime_judgehost_status" not in payload:
        try:
            payload["runtime_judgehost_status"] = runtime().judgehost_task_service.public_status()
        except Exception:
            payload["runtime_judgehost_status"] = {
                "enabled": False,
                "hosts_online": 0,
                "hosts_total": 0,
                "queued": 0,
                "busy_hosts": 0,
                "summary": "offline",
                "tone": "danger",
                "hosts": [],
                "compile_specs": [],
                "toolchains": [],
                "toolchain_mismatch": False,
            }
    backend_render_ms: int | None = None
    started = getattr(request.state, "request_started_at", None)
    if isinstance(started, (int, float)):
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms >= 0:
            backend_render_ms = int(round(elapsed_ms))
    if "backend_render_ms" not in payload:
        payload["backend_render_ms"] = backend_render_ms
    raw_cookie = str(
        request.cookies.get(runtime().config_values.text("FLASH_COOKIE_NAME"), "")
        or ""
    ).strip()
    queue = _decode_flash_queue(raw_cookie)
    fallback_message = _normalize_flash_message(payload.get("message", ""))
    ctx = payload.get("ctx")
    auto_update_message = ""
    if isinstance(ctx, dict):
        problem = ctx.get("problem")
        if isinstance(problem, dict) and isinstance(problem.get("slug"), str):
            payload.update(problem_template_navigation(request, problem["slug"]))
        auto_update_message = _normalize_flash_message(
            ctx.get("workspace_auto_update_message", "")
        )
    message = auto_update_message or (queue[0] if queue else fallback_message)
    message_ts = int(time.time() * 1000)
    payload["message"] = message
    payload["message_level"] = _flash_message_level(message)
    payload["message_source"] = str(template_name or "").strip()
    payload["message_event_id"] = _flash_message_event_id(message, scope=f"{payload['message_source']}:{message_ts}")
    payload["message_ts"] = message_ts
    response = runtime().templates.TemplateResponse(request, template_name, payload)
    if auto_update_message and queue:
        # The update happened while rendering this response. Show it now without
        # consuming an older redirect message that still belongs to the user.
        set_flash_cookie(response, queue)
    elif queue:
        set_flash_cookie(response, queue[1:])
    elif raw_cookie:
        set_flash_cookie(response, [])
    _apply_security_headers(response)
    return response


def normalize_password_salt_hex(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not _K.HEX_32_RE.fullmatch(raw):
        raise ValueError("invalid password salt")
    return raw


def normalize_password_verifier_hex(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not _K.HEX_64_RE.fullmatch(raw):
        raise ValueError("invalid password verifier")
    return raw


def normalize_password_iters(value: object) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            iters = value
        elif isinstance(value, str):
            iters = int(value)
        else:
            raise ValueError
    except Exception as exc:
        raise ValueError("invalid password iterations") from exc
    if iters < 10000 or iters > 10000000:
        raise ValueError("invalid password iterations")
    return iters


def dummy_password_salt_hex(username: str) -> str:
    safe_user = str(username or "").strip().lower()
    digest = hmac_sha256_hex(runtime().password_form_csrf_secret, f"dummy-meta|{safe_user}".encode("utf-8"))
    return digest[:32]


def password_meta_for_username(username: str) -> tuple[str, int]:
    row = lookup_user_auth(username)
    if row is None:
        return (
            dummy_password_salt_hex(username),
            runtime().config_values.integer("PASSWORD_HASH_ITERS"),
        )
    stored_hash = str(row["password_hash"] or "").strip().lower()
    salt_hex = str(row["password_salt"] or "").strip().lower()
    try:
        iterations = int(row["password_iters"] or 0)
    except Exception:
        iterations = 0
    if _K.HEX_64_RE.fullmatch(stored_hash) and _K.HEX_32_RE.fullmatch(salt_hex) and (iterations > 0):
        return (salt_hex, iterations)
    return (
        dummy_password_salt_hex(username),
        runtime().config_values.integer("PASSWORD_HASH_ITERS"),
    )


def lookup_user_auth(username: str):
    safe = str(username or "").strip()
    if len(safe) < _K.USERNAME_MIN_LEN or len(safe) > _K.USERNAME_MAX_LEN or not _K.USER_IDENT_RE.fullmatch(safe):
        return None
    return runtime().auth_service.lookup_user_auth(safe)


def _registered_user_count() -> int:
    return (1 if runtime().auth_service.has_registered_users() else 0)


def has_registered_users() -> bool:
    return _registered_user_count() > 0


def normalize_username_required(value: str) -> str:
    safe = str(value or "").strip()
    if len(safe) < _K.USERNAME_MIN_LEN or len(safe) > _K.USERNAME_MAX_LEN or not _K.USER_IDENT_RE.fullmatch(safe):
        raise ValueError(_K.USERNAME_RULE_MESSAGE)
    return safe


def set_user_password_verifier(user_id: int, verifier_hex: str, salt_hex: str, iterations: int) -> None:
    safe_verifier = normalize_password_verifier_hex(verifier_hex)
    safe_salt = normalize_password_salt_hex(salt_hex)
    safe_iters = normalize_password_iters(iterations)
    runtime().auth_service.set_user_password_verifier(
        user_id=int(user_id),
        verifier_hex=safe_verifier,
        salt_hex=safe_salt,
        iterations=safe_iters,
    )


def create_user_with_password_verifier(
    username: str,
    verifier_hex: str,
    salt_hex: str,
    iterations: int,
    *,
    email: str = "",
    email_normalized: str = "",
    email_verified_at: str = "",
) -> int:
    safe_user = normalize_username_required(username)
    safe_verifier = normalize_password_verifier_hex(verifier_hex)
    safe_salt = normalize_password_salt_hex(salt_hex)
    safe_iters = normalize_password_iters(iterations)
    return runtime().auth_service.create_user_with_password_verifier(
        username=safe_user,
        verifier_hex=safe_verifier,
        salt_hex=safe_salt,
        iterations=safe_iters,
        email=email,
        email_normalized=email_normalized,
        email_verified_at=email_verified_at,
    )


def bootstrap_super_admin_with_password_verifier(
    username: str,
    email: str,
    email_normalized: str,
    verifier_hex: str,
    salt_hex: str,
    iterations: int,
    email_allow_regex: str,
    email_allow_regex_default: str,
) -> int:
    safe_user = normalize_username_required(username)
    safe_verifier = normalize_password_verifier_hex(verifier_hex)
    safe_salt = normalize_password_salt_hex(salt_hex)
    safe_iters = normalize_password_iters(iterations)
    return runtime().auth_service.bootstrap_super_admin_with_password_verifier(
        username=safe_user,
        email=email,
        email_normalized=email_normalized,
        verifier_hex=safe_verifier,
        salt_hex=safe_salt,
        iterations=safe_iters,
        email_allow_regex=email_allow_regex,
        email_allow_regex_default=email_allow_regex_default,
    )


def safe_next_path(raw: str | None, fallback: str = "/") -> str:
    candidate = str(raw or "").strip()
    if not candidate:
        return fallback
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    return candidate


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += f"?{request.url.query}"
    if not has_registered_users():
        return redirect_response(f"/setup?next={quote_plus(target)}", status_code=303)
    return redirect_response(f"/login?next={quote_plus(target)}", status_code=303)


def _request_origin_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    scheme = str(parsed.scheme or "").strip().lower()
    netloc = str(parsed.netloc or "").strip().lower()
    if not scheme or not netloc:
        return ""
    return f"{scheme}://{netloc}"


def _expected_request_origin(request: Request) -> str:
    return f"{str(request.url.scheme).strip().lower()}://{str(request.url.netloc).strip().lower()}"


def enforce_same_origin_state_change(request: Request | None) -> None:
    if request is None:
        return
    method = str(request.method or "").strip().upper()
    if method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return
    expected = _expected_request_origin(request)
    origin = _request_origin_value(str(request.headers.get("origin") or ""))
    if origin:
        if origin != expected:
            raise HTTPException(status_code=403, detail="cross-site request blocked")
        return
    referer = _request_origin_value(str(request.headers.get("referer") or ""))
    if referer:
        if referer != expected:
            raise HTTPException(status_code=403, detail="cross-site request blocked")
        return
    raise HTTPException(status_code=403, detail="missing origin/referrer for state-changing request")


def login_rate_limit_key(username: str, request: Request | None) -> str:
    safe_user = str(username or "").strip().lower()
    ip = ""
    if request is not None:
        forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            ip = str(forwarded.split(",", 1)[0]).strip()
        if not ip:
            client = request.client
            ip = str(client.host).strip() if client is not None and client.host else ""
    if not ip:
        ip = "unknown"
    return f"{ip}|{safe_user}"


def login_rate_limit_check(key: str) -> None:
    now_monotonic = time.monotonic()
    with runtime().login_rate_limit_lock:
        state = runtime().login_rate_limit_state.get(key)
        if state is None:
            return
        blocked_until = float(state.get("blocked_until") or 0.0)
        if blocked_until > now_monotonic:
            wait_sec = max(1, int(round(blocked_until - now_monotonic)))
            raise ValueError(f"too many failed attempts; retry in {wait_sec}s")
        window_start = float(state.get("window_start") or 0.0)
        if (
            window_start <= 0.0
            or now_monotonic - window_start
            > runtime().config_values.floating("LOGIN_RATE_LIMIT_WINDOW_SEC")
        ):
            runtime().login_rate_limit_state.pop(key, None)


def login_rate_limit_fail(key: str) -> None:
    now_monotonic = time.monotonic()
    with runtime().login_rate_limit_lock:
        state = runtime().login_rate_limit_state.get(key)
        if state is None:
            state = {"window_start": now_monotonic, "failures": 0, "blocked_until": 0.0}
        window_start = float(state.get("window_start") or 0.0)
        if (
            window_start <= 0.0
            or now_monotonic - window_start
            > runtime().config_values.floating("LOGIN_RATE_LIMIT_WINDOW_SEC")
        ):
            state = {"window_start": now_monotonic, "failures": 0, "blocked_until": 0.0}
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        if failures >= runtime().config_values.integer(
            "LOGIN_RATE_LIMIT_MAX_FAILURES"
        ):
            state["blocked_until"] = now_monotonic + runtime().config_values.floating(
                "LOGIN_RATE_LIMIT_BLOCK_SEC"
            )
            state["window_start"] = now_monotonic
            state["failures"] = 0
        runtime().login_rate_limit_state[key] = state


def login_rate_limit_success(key: str) -> None:
    with runtime().login_rate_limit_lock:
        runtime().login_rate_limit_state.pop(key, None)
