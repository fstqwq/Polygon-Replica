from __future__ import annotations
import base64
import json
import platform
import re
import secrets
import shutil
import sqlite3
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from app.impl.config import config
from app.db import now_iso
from app.services.hashing import hmac_sha256_hex, sha256_hex_bytes, sha256_hex_text

_C = config.constants
_RUNTIME_PROFILE_CACHE: dict[str, str] | None = None
_RUNTIME_PROFILE_MAX_LEN = 160
_RUNTIME_BACKEND_CACHE: dict[str, str] | None = None
_RUNTIME_BACKEND_CACHE_TS = 0.0
_RUNTIME_BACKEND_CACHE_TTL_SEC = 2.0


def _sanitize_runtime_profile_value(raw: object, default: str = "n/a") -> str:
    text = " ".join(str(raw or "").split()).strip()
    if not text:
        return default
    if len(text) > _RUNTIME_PROFILE_MAX_LEN:
        return text[: _RUNTIME_PROFILE_MAX_LEN - 3].rstrip() + "..."
    return text


def _read_linux_distro_label() -> str:
    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="replace") as fh:
            values: dict[str, str] = {}
            for line in fh:
                raw = str(line or "").strip()
                if not raw or "=" not in raw or raw.startswith("#"):
                    continue
                key, value = raw.split("=", 1)
                values[str(key or "").strip()] = str(value or "").strip().strip('"').strip("'")
    except Exception:
        values = {}
    return str(values.get("PRETTY_NAME") or values.get("NAME") or "").strip()


def _parse_cpu_frequency_ghz(raw: object) -> float | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    match_ghz = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*ghz", text)
    if match_ghz is not None:
        try:
            value = float(match_ghz.group(1))
            if value > 0:
                return value
        except Exception:
            return None
    match_mhz = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*mhz", text)
    if match_mhz is not None:
        try:
            value = float(match_mhz.group(1)) / 1000.0
            if value > 0:
                return value
        except Exception:
            return None
    try:
        numeric = float(text)
    except Exception:
        return None
    if numeric > 100:
        return numeric / 1000.0
    if numeric > 0:
        return numeric
    return None


def _format_cpu_label(label: str, freq_ghz: float | None) -> str:
    text = " ".join(str(label or "").split()).strip()
    if not text:
        return ""
    if freq_ghz is None:
        freq_ghz = _parse_cpu_frequency_ghz(text)
    if freq_ghz is None or freq_ghz <= 0:
        return text
    suffix = f" @{freq_ghz:.2f}GHz"
    if re.search(r"@\s*[0-9]+(?:\.[0-9]+)?\s*ghz", text, flags=re.IGNORECASE):
        return re.sub(r"@\s*[0-9]+(?:\.[0-9]+)?\s*ghz", suffix, text, count=1, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[0-9]+(?:\.[0-9]+)?\s*ghz\b", "", text, flags=re.IGNORECASE).strip()
    if not cleaned:
        cleaned = text
    return f"{cleaned}{suffix}"


def _read_cpu_info_details() -> tuple[str, float | None]:
    try:
        primary = ""
        secondary = ""
        freq_ghz: float | None = None
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = str(line or "")
                if ":" not in text:
                    continue
                key, value = text.split(":", 1)
                k = str(key or "").strip().lower()
                cleaned = str(value or "").strip()
                if not cleaned:
                    continue
                if k in {"model name", "cpu model", "hardware"}:
                    if (not primary) and (not re.fullmatch(r"\d+", cleaned)):
                        primary = cleaned
                    continue
                if k == "processor" and (not secondary) and (not re.fullmatch(r"\d+", cleaned)):
                    secondary = cleaned
                    continue
                if (k in {"cpu mhz", "clock"}) and (freq_ghz is None):
                    freq_ghz = _parse_cpu_frequency_ghz(cleaned)
        if primary:
            return (primary, freq_ghz)
        if secondary:
            return (secondary, freq_ghz)
    except Exception:
        pass
    return ("", None)


def _runtime_footer_profile() -> dict[str, str]:
    global _RUNTIME_PROFILE_CACHE
    if isinstance(_RUNTIME_PROFILE_CACHE, dict):
        return dict(_RUNTIME_PROFILE_CACHE)
    distro = _read_linux_distro_label()
    if not distro:
        distro = f"{platform.system()} {platform.release()}".strip()
    cpu_name, cpu_ghz = _read_cpu_info_details()
    if not cpu_name:
        cpu_name = str(platform.processor() or platform.machine() or "").strip()
    cpu = _format_cpu_label(cpu_name, cpu_ghz)
    profile = {
        "runtime_linux_distro": _sanitize_runtime_profile_value(distro),
        "runtime_cpu_info": _sanitize_runtime_profile_value(cpu),
    }
    _RUNTIME_PROFILE_CACHE = dict(profile)
    return profile


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _runtime_backend_profile() -> dict[str, str]:
    global _RUNTIME_BACKEND_CACHE, _RUNTIME_BACKEND_CACHE_TS
    now = time.monotonic()
    if (
        isinstance(_RUNTIME_BACKEND_CACHE, dict)
        and (now - float(_RUNTIME_BACKEND_CACHE_TS)) <= _RUNTIME_BACKEND_CACHE_TTL_SEC
    ):
        return dict(_RUNTIME_BACKEND_CACHE)
    sandbox_name = _sanitize_runtime_profile_value(getattr(config.sandbox_backend, "name", ""), "n/a")
    sandbox_count = "1" if sandbox_name != "n/a" else "0"
    judgehost_enabled = False
    hosts_online = 0
    hosts_total = 0
    queued = 0
    leased = 0
    completed = 0
    failed = 0
    try:
        status = config.judgehost_task_service.status()
        if isinstance(status, dict):
            judgehost_enabled = bool(status.get("enabled"))
            hosts_online = _safe_int(status.get("hosts_online"), 0)
            hosts_total = _safe_int(status.get("hosts_total"), 0)
            queue = status.get("queue")
            if isinstance(queue, dict):
                queued = _safe_int(queue.get("queued"), 0)
                leased = _safe_int(queue.get("leased"), 0)
                completed = _safe_int(queue.get("completed"), 0)
                failed = _safe_int(queue.get("failed"), 0)
    except Exception:
        pass
    hosts_online = max(0, int(hosts_online))
    hosts_total = max(0, int(hosts_total))
    queued = max(0, int(queued))
    leased = max(0, int(leased))
    completed = max(0, int(completed))
    failed = max(0, int(failed))
    if judgehost_enabled:
        judgehost_summary = (
            f"online {hosts_online}/{hosts_total}; "
            f"queued={queued}; leased={leased}; completed={completed}; failed={failed}"
        )
    else:
        judgehost_summary = "disabled"
    judgehost_danger = (not judgehost_enabled) or (hosts_online <= 0)
    profile = {
        "runtime_sandbox_backend": sandbox_name,
        "runtime_sandbox_backend_count": sandbox_count,
        "runtime_judgehost_backend_summary": _sanitize_runtime_profile_value(judgehost_summary),
        "runtime_judgehost_backend_danger": "1" if judgehost_danger else "0",
        "runtime_judgehost_enabled": "1" if judgehost_enabled else "0",
        "runtime_judgehost_hosts_online": str(hosts_online),
        "runtime_judgehost_hosts_total": str(hosts_total),
    }
    _RUNTIME_BACKEND_CACHE = dict(profile)
    _RUNTIME_BACKEND_CACHE_TS = now
    return profile

def _startup_cancel_summary_rows(table_name: str, reason: str, *, now_text: str) -> None:
    safe_table = str(table_name or "").strip()
    if safe_table not in {"builds", "previews", "runs", "contest_jobs"}:
        return
    try:
        rows = config.db.fetch_all(
            f"SELECT id,summary_json FROM {safe_table} WHERE status IN ('running','queued','pending')"
        )
    except Exception as exc:
        warnings.warn(f"startup {safe_table} inflight scan failed: {exc}", RuntimeWarning)
        return
    for row in rows:
        row_id = str(row["id"] or "").strip()
        if not row_id:
            continue
        summary_obj: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["summary_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                summary_obj = dict(parsed)
        except Exception:
            summary_obj = {}
        summary_obj["cancelled"] = True
        summary_obj["cancel_reason"] = reason
        if not str(summary_obj.get("error") or "").strip():
            summary_obj["error"] = reason
        try:
            config.db.execute(
                f"""
                UPDATE {safe_table}
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=?
                """,
                [json.dumps(summary_obj), now_text, row_id],
            )
        except Exception as exc:
            warnings.warn(f"startup {safe_table} inflight cancel failed for {row_id}: {exc}", RuntimeWarning)


def _startup_cancel_judgehost_inflight(reason: str, *, now_text: str) -> None:
    run_ids: list[str] = []
    service = getattr(config, "judgehost_task_service", None)
    if service is not None:
        try:
            run_ids = list(service.startup_cancel_inflight_tasks(reason=reason))
        except Exception as exc:
            warnings.warn(f"startup judgehost inflight scan failed: {exc}", RuntimeWarning)
    if service is not None:
        try:
            service.cancel_all_domjudge_inflight()
        except Exception as exc:
            warnings.warn(f"startup judgehost job/case cancel failed: {exc}", RuntimeWarning)
    if not run_ids:
        return
    placeholders = ",".join(("?" for _ in run_ids))
    try:
        run_rows = config.db.fetch_all(
            f"SELECT id,summary_json,status FROM runs WHERE id IN ({placeholders})",
            [*run_ids],
        )
    except Exception as exc:
        warnings.warn(f"startup judgehost run scan failed: {exc}", RuntimeWarning)
        return
    for run_row in run_rows:
        run_id = str(run_row["id"] or "").strip()
        if not run_id:
            continue
        status = str(run_row["status"] or "").strip().lower()
        if status not in {"running", "queued", "pending"}:
            continue
        summary_obj: dict[str, object] = {}
        try:
            parsed = json.loads(str(run_row["summary_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                summary_obj = dict(parsed)
        except Exception:
            summary_obj = {}
        summary_obj["cancelled"] = True
        summary_obj["cancel_reason"] = reason
        if not str(summary_obj.get("error") or "").strip():
            summary_obj["error"] = reason
        try:
            config.db.execute(
                """
                UPDATE runs
                SET status='failed', summary_json=?, finished_at=COALESCE(finished_at, ?)
                WHERE id=?
                """,
                [json.dumps(summary_obj), now_text, run_id],
            )
        except Exception as exc:
            warnings.warn(f"startup run cancel failed for {run_id}: {exc}", RuntimeWarning)


def _startup_normalize_run_token(raw: object) -> str:
    token = str(raw or "").strip()
    if not token:
        return ""
    token = token.split("?", 1)[0].split("#", 1)[0].strip()
    if not token:
        return ""
    if "/" in token:
        token = token.rsplit("/", 1)[-1].strip()
    return token


def _startup_collect_invocation_run_ids(details: dict[str, object]) -> list[str]:
    values: list[str] = []
    primary = _startup_normalize_run_token(details.get("run_id"))
    if primary:
        values.append(primary)
    raw_run_ids = details.get("run_ids")
    if isinstance(raw_run_ids, list):
        for raw in raw_run_ids:
            token = _startup_normalize_run_token(raw)
            if token:
                values.append(token)
    seen: set[str] = set()
    deduped: list[str] = []
    for token in values:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _startup_cancel_audit_inflight(reason: str, *, now_text: str) -> None:
    try:
        rows = config.db.fetch_all(
            """
            SELECT id,actor_user_id,problem_id,action,details_json
            FROM audit_log
            WHERE action IN ('run.execute', 'verification.start', 'run.cancel')
            ORDER BY created_at DESC, id DESC
            """
        )
    except Exception as exc:
        warnings.warn(f"startup audit inflight scan failed: {exc}", RuntimeWarning)
        return
    seen: set[tuple[int, int, str]] = set()
    pending_cancel_rows: list[tuple[int | None, int | None, dict[str, object]]] = []
    for row in rows:
        details: dict[str, object] = {}
        try:
            parsed = json.loads(str(row["details_json"] or "").strip() or "{}")
            if isinstance(parsed, dict):
                details = dict(parsed)
        except Exception:
            details = {}
        invocation_id = _startup_normalize_run_token(details.get("invocation_id"))
        if not invocation_id:
            continue
        try:
            actor_user_id = int(row["actor_user_id"]) if row["actor_user_id"] is not None else None
        except Exception:
            actor_user_id = None
        try:
            problem_id = int(row["problem_id"]) if row["problem_id"] is not None else None
        except Exception:
            problem_id = None
        scope_key = (int(problem_id or -1), int(actor_user_id or -1), invocation_id)
        if scope_key in seen:
            continue
        seen.add(scope_key)
        action_token = str(row["action"] or "").strip().lower()
        if action_token == "run.cancel":
            continue
        status_token = str(details.get("status") or "").strip().lower()
        if status_token not in {"running", "queued", "pending"}:
            continue
        invocation_run_ids = _startup_collect_invocation_run_ids(details)
        cancel_details: dict[str, object] = {
            "invocation_id": invocation_id,
            "run_ids": invocation_run_ids,
            "run_count": len(invocation_run_ids),
            "cancelled_runs": 0,
            "cancelled_tasks": 0,
            "cancelled_builds": 0,
            "reason": reason,
        }
        pending_cancel_rows.append((actor_user_id, problem_id, cancel_details))
    for actor_user_id, problem_id, cancel_details in pending_cancel_rows:
        try:
            config.db.execute(
                """
                INSERT INTO audit_log(actor_user_id, problem_id, action, details_json, created_at)
                VALUES(?, ?, 'run.cancel', ?, ?)
                """,
                [
                    actor_user_id,
                    problem_id,
                    json.dumps(cancel_details),
                    now_text,
                ],
            )
        except Exception as exc:
            invocation_id = str(cancel_details.get("invocation_id") or "").strip()
            warnings.warn(f"startup audit inflight cancel failed for {invocation_id}: {exc}", RuntimeWarning)


def _startup_clear_all_caches() -> None:
    try:
        config.async_task_cache_service.clear_all()
    except Exception as exc:
        warnings.warn(f"startup async cache clear failed: {exc}", RuntimeWarning)
    try:
        config.judge_fs_index_service.clear_all()
    except Exception as exc:
        warnings.warn(f"startup judge fs index clear failed: {exc}", RuntimeWarning)
    testcase_cache_root = (config.settings.cache_root / "judgehost-domjudge-testcases").resolve()
    try:
        if testcase_cache_root.exists() and testcase_cache_root.is_dir() and (not testcase_cache_root.is_symlink()):
            shutil.rmtree(testcase_cache_root, ignore_errors=True)
    except Exception as exc:
        warnings.warn(f"startup testcase cache clear failed: {exc}", RuntimeWarning)
    try:
        testcase_cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        config.judgehost_task_service.clear_testcase_registry()
    except Exception as exc:
        warnings.warn(f"startup testcase registry reset failed: {exc}", RuntimeWarning)
    durable_log_raw = str(_C.WORKER_QUEUE_DURABLE_LOG or "").strip()
    durable_log = (config.settings.cache_root / "worker-queue-events.jsonl").resolve()
    if durable_log_raw:
        durable_log = Path(durable_log_raw).expanduser().resolve()
    try:
        durable_log.unlink(missing_ok=True)
    except Exception as exc:
        warnings.warn(f"startup worker queue durable log clear failed: {exc}", RuntimeWarning)


def _startup_reset_runtime_state() -> None:
    now_text = now_iso()
    cancel_reason = "cancelled on service startup"
    _startup_cancel_summary_rows("builds", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("previews", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("runs", cancel_reason, now_text=now_text)
    _startup_cancel_summary_rows("contest_jobs", cancel_reason, now_text=now_text)
    _startup_cancel_judgehost_inflight(cancel_reason, now_text=now_text)
    _startup_cancel_audit_inflight(cancel_reason, now_text=now_text)
    _startup_clear_all_caches()


def startup() -> None:
    config.db.init()
    _startup_reset_runtime_state()
    config.invocation_backend_service.refresh()
    config.worker_queue_service.start()


def shutdown() -> None:
    try:
        config.worker_queue_service.stop()
    except Exception as exc:
        warnings.warn(f'shutdown worker queue stop failed: {exc}', RuntimeWarning)

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _parse_iso_utc(raw: str) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
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
            return raw.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        except Exception:
            return raw.strftime('%Y-%m-%d %H:%M:%S')
    text = str(raw or '').strip()
    if not text:
        return '-'
    parsed = _parse_iso_utc(text)
    if parsed is None:
        return text
    try:
        return parsed.astimezone().strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return parsed.strftime('%Y-%m-%d %H:%M:%S')
config.templates.env.filters['local_time'] = _format_local_time

_STATUS_LABEL_MAP: dict[str, str] = {
    'ok': 'OK',
    'success': 'SUCCESS',
    'failed': 'FAILED',
    'error': 'ERROR',
    'running': 'RUNNING',
    'queued': 'QUEUED',
    'pending': 'PENDING',
    'stale': 'STALE',
    'missing': 'MISSING',
    'invalid': 'INVALID',
    'none': 'NONE',
    'ready': 'READY',
    'not_ready': 'NOT READY',
    'online': 'ONLINE',
    'offline': 'OFFLINE',
    'ac': 'AC',
    'wa': 'WA',
    'pe': 'PE',
    're': 'RE',
    'tle': 'TLE',
    'mle': 'MLE',
    'ce': 'CE',
    'se': 'SE',
}

def _format_status_label(raw: object) -> str:
    text = str(raw or '').strip()
    if not text:
        return '-'
    normalized = re.sub(r'[\s\-]+', '_', text.lower()).strip('_')
    if not normalized:
        return '-'
    mapped = _STATUS_LABEL_MAP.get(normalized)
    if mapped:
        return mapped
    tokens = [tok for tok in re.split(r'[_\s\-]+', normalized) if tok]
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
    return ' '.join(words)
config.templates.env.filters['status_label'] = _format_status_label

def _normalize_flash_message(raw: object) -> str:
    text = str(raw or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return ''
    # Preserve line breaks for compiler/runtime diagnostics while normalizing spacing per line.
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.split('\n')]
    lines = [line for line in lines if line]
    text = '\n'.join(lines).strip()
    # Keep wording stable, but normalize ending punctuation for consistent notice style.
    if text and text[-1] not in '.!?':
        if re.search(r'[A-Za-z0-9\)]$', text):
            text += '.'
    if len(text) > _C.FLASH_MESSAGE_MAX_LEN:
        text = text[:_C.FLASH_MESSAGE_MAX_LEN].rstrip()
    return text

def _flash_message_level(raw: object) -> str:
    text = str(raw or '').strip().lower()
    if not text:
        return 'info'
    if any((token in text for token in {'error', 'failed', 'invalid', 'denied', 'rejected'})):
        return 'error'
    if any((token in text for token in {'warning', 'stale', 'already running', 'already exists'})):
        return 'warning'
    if any((token in text for token in {'saved', 'created', 'updated', 'queued', 'running', 'ok', 'done', 'success'})):
        return 'success'
    return 'info'

def _flash_message_event_id(message: str, *, scope: str = '') -> str:
    normalized = _normalize_flash_message(message)
    if not normalized:
        return ''
    payload = f'{str(scope or "").strip()}|{normalized}'.encode('utf-8')
    return sha256_hex_bytes(payload)[:16]

def _decode_flash_queue(raw_cookie: str) -> list[str]:
    token = str(raw_cookie or '').strip()
    if not token:
        return []
    try:
        padded = token + '=' * ((4 - len(token) % 4) % 4)
        payload = base64.urlsafe_b64decode(padded.encode('ascii')).decode('utf-8')
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
        if len(queue) >= _C.FLASH_QUEUE_MAX_ITEMS:
            break
    return queue

def _encode_flash_queue(queue: list[str]) -> str:
    safe_items: list[str] = []
    for item in queue:
        normalized = _normalize_flash_message(item)
        if not normalized:
            continue
        safe_items.append(normalized)
        if len(safe_items) >= _C.FLASH_QUEUE_MAX_ITEMS:
            break
    if not safe_items:
        return ''
    payload = json.dumps(safe_items, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')

def _set_flash_cookie(response, queue: list[str]) -> None:
    encoded = _encode_flash_queue(queue)
    if not encoded:
        response.delete_cookie(_C.FLASH_COOKIE_NAME, path='/', secure=_C.AUTH_COOKIE_SECURE, httponly=True, samesite='lax')
        return
    response.set_cookie(_C.FLASH_COOKIE_NAME, encoded, httponly=True, samesite='lax', secure=_C.AUTH_COOKIE_SECURE, max_age=_C.FLASH_COOKIE_MAX_AGE, path='/')

def _sanitize_redirect_target(target: str) -> str:
    url = str(target or '').strip() or '/'
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key == 'message':
            continue
        kept.append((key, value))
    cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(kept, doseq=True), parsed.fragment))
    if parsed.scheme or parsed.netloc:
        return cleaned or url
    if not cleaned:
        return url
    return cleaned

def _apply_security_headers(response) -> None:
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")

def _redirect_response(url: str, status_code: int=303, message: str='') -> RedirectResponse:
    target = _sanitize_redirect_target(url)
    response = RedirectResponse(target, status_code=status_code)
    safe_message = _normalize_flash_message(message)
    if safe_message:
        _set_flash_cookie(response, [safe_message])
    _apply_security_headers(response)
    return response

def _template_response(request: Request, template_name: str, context: dict | None=None):
    payload = dict(context or {})
    runtime_profile = _runtime_footer_profile()
    runtime_backend = _runtime_backend_profile()
    if "runtime_linux_distro" not in payload:
        payload["runtime_linux_distro"] = runtime_profile.get("runtime_linux_distro", "n/a")
    if "runtime_cpu_info" not in payload:
        payload["runtime_cpu_info"] = runtime_profile.get("runtime_cpu_info", "n/a")
    if "runtime_sandbox_backend" not in payload:
        payload["runtime_sandbox_backend"] = runtime_backend.get("runtime_sandbox_backend", "n/a")
    if "runtime_sandbox_backend_count" not in payload:
        payload["runtime_sandbox_backend_count"] = runtime_backend.get("runtime_sandbox_backend_count", "0")
    if "runtime_judgehost_backend_summary" not in payload:
        payload["runtime_judgehost_backend_summary"] = runtime_backend.get(
            "runtime_judgehost_backend_summary",
            "disabled",
        )
    if "runtime_judgehost_backend_danger" not in payload:
        payload["runtime_judgehost_backend_danger"] = runtime_backend.get(
            "runtime_judgehost_backend_danger",
            "1",
        )
    if "runtime_judgehost_enabled" not in payload:
        payload["runtime_judgehost_enabled"] = runtime_backend.get("runtime_judgehost_enabled", "0")
    if "runtime_judgehost_hosts_online" not in payload:
        payload["runtime_judgehost_hosts_online"] = runtime_backend.get("runtime_judgehost_hosts_online", "0")
    if "runtime_judgehost_hosts_total" not in payload:
        payload["runtime_judgehost_hosts_total"] = runtime_backend.get("runtime_judgehost_hosts_total", "0")
    backend_render_ms: int | None = None
    started = getattr(request.state, 'request_started_at', None)
    if isinstance(started, (int, float)):
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms >= 0:
            backend_render_ms = int(round(elapsed_ms))
    if 'backend_render_ms' not in payload:
        payload['backend_render_ms'] = backend_render_ms
    raw_cookie = str(request.cookies.get(_C.FLASH_COOKIE_NAME, '') or '').strip()
    queue = _decode_flash_queue(raw_cookie)
    fallback_message = _normalize_flash_message(payload.get('message', ''))
    message = queue[0] if queue else fallback_message
    message_ts = int(time.time() * 1000)
    payload['message'] = message
    payload['message_level'] = _flash_message_level(message)
    payload['message_source'] = str(template_name or '').strip()
    payload['message_event_id'] = _flash_message_event_id(message, scope=f"{payload['message_source']}:{message_ts}")
    payload['message_ts'] = message_ts
    response = config.templates.TemplateResponse(request, template_name, payload)
    if queue:
        _set_flash_cookie(response, queue[1:])
    elif raw_cookie:
        _set_flash_cookie(response, [])
    _apply_security_headers(response)
    return response

def _normalize_password_salt_hex(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not _C.HEX_32_RE.fullmatch(raw):
        raise ValueError('invalid password salt')
    return raw

def _normalize_password_verifier_hex(value: str) -> str:
    raw = str(value or '').strip().lower()
    if not _C.HEX_64_RE.fullmatch(raw):
        raise ValueError('invalid password verifier')
    return raw

def _normalize_password_iters(value: object) -> int:
    try:
        iters = int(value)
    except Exception as exc:
        raise ValueError('invalid password iterations') from exc
    if iters < 10000 or iters > 10000000:
        raise ValueError('invalid password iterations')
    return iters

def _password_form_csrf_signature(scope: str, issued_at: int, nonce: str) -> str:
    payload = f'{scope}|{issued_at}|{nonce}'.encode('utf-8')
    return hmac_sha256_hex(config.password_form_csrf_secret, payload)

def _issue_password_form_csrf_token(scope: str) -> str:
    safe_scope = str(scope or '').strip().lower()
    issued_at = int(time.time())
    nonce = secrets.token_hex(16)
    signature = _password_form_csrf_signature(safe_scope, issued_at, nonce)
    return f'{issued_at}.{nonce}.{signature}'

def _verify_password_form_csrf_token(token: str, scope: str) -> bool:
    raw = str(token or '').strip()
    if not raw:
        return False
    parts = raw.split('.')
    if len(parts) != 3:
        return False
    issued_raw, nonce, provided_sig = parts
    if not _C.HEX_32_RE.fullmatch(str(nonce or '').lower()):
        return False
    if not _C.HEX_64_RE.fullmatch(str(provided_sig or '').lower()):
        return False
    try:
        issued_at = int(issued_raw)
    except Exception:
        return False
    now_ts = int(time.time())
    if issued_at <= 0 or issued_at > now_ts + 60:
        return False
    if now_ts - issued_at > _C.PASSWORD_FORM_CSRF_TTL_SEC:
        return False
    expected = _password_form_csrf_signature(str(scope or '').strip().lower(), issued_at, nonce)
    return secrets.compare_digest(expected, str(provided_sig or '').lower())

def _password_proof_from_verifier(csrf_token: str, verifier_hex: str) -> str:
    safe_csrf = str(csrf_token or '').strip()
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    digest = sha256_hex_text(f'{safe_csrf}{safe_verifier}')
    return digest

def _dummy_password_salt_hex(username: str) -> str:
    safe_user = str(username or '').strip().lower()
    digest = hmac_sha256_hex(config.password_form_csrf_secret, f'dummy-meta|{safe_user}'.encode('utf-8'))
    return digest[:32]

def _password_meta_for_username(username: str) -> tuple[str, int]:
    row = _lookup_user_auth(username)
    if row is None:
        return (_dummy_password_salt_hex(username), int(_C.PASSWORD_HASH_ITERS))
    verifier = str(row['password_hash'] or '').strip().lower()
    salt_hex = str(row['password_salt'] or '').strip().lower()
    try:
        iterations = int(row['password_iters'] or 0)
    except Exception:
        iterations = 0
    if _C.HEX_64_RE.fullmatch(verifier) and _C.HEX_32_RE.fullmatch(salt_hex) and (iterations > 0):
        return (salt_hex, iterations)
    return (_dummy_password_salt_hex(username), int(_C.PASSWORD_HASH_ITERS))

def _lookup_user_auth(username: str):
    safe = str(username or '').strip()
    if not _C.USER_IDENT_RE.fullmatch(safe):
        return None
    return config.db.fetch_one('SELECT id,username,password_hash,password_salt,password_iters FROM users WHERE username=?', [safe])

def _registered_user_count() -> int:
    row = config.db.fetch_one("SELECT COUNT(*) AS c FROM users WHERE COALESCE(TRIM(password_hash), '') <> ''", [])
    if row is None:
        return 0
    try:
        return max(0, int(row['c'] or 0))
    except Exception:
        return 0

def _has_registered_users() -> bool:
    return _registered_user_count() > 0

def _normalize_username_required(value: str) -> str:
    safe = str(value or '').strip()
    if len(safe) > 64 or not _C.USER_IDENT_RE.fullmatch(safe):
        raise ValueError(_C.USERNAME_RULE_MESSAGE)
    return safe

def _set_user_password_verifier(user_id: int, verifier_hex: str, salt_hex: str, iterations: int) -> None:
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    config.db.execute('UPDATE users SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=? WHERE id=?', [safe_verifier, safe_salt, safe_iters, now_iso(), int(user_id)])

def _create_user_with_password_verifier(username: str, verifier_hex: str, salt_hex: str, iterations: int) -> int:
    safe_user = _normalize_username_required(username)
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    now = now_iso()

    def _tx(conn: sqlite3.Connection) -> int:
        has_registered_user = conn.execute("SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1").fetchone() is not None
        admin_candidates = [0] if has_registered_user else [1, 0]
        inserted = False
        for is_admin in admin_candidates:
            try:
                conn.execute('\n                    INSERT INTO users(\n                        username,password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin\n                    )\n                    VALUES(?,?,?,?,?,?,?)\n                    ', [safe_user, safe_verifier, safe_salt, safe_iters, now, now, int(is_admin)])
                inserted = True
                break
            except sqlite3.IntegrityError as exc:
                msg = str(exc or '').strip().lower()
                if 'users.username' in msg:
                    raise ValueError('user already exists') from exc
                if int(is_admin) == 1:
                    continue
                raise
        if not inserted:
            raise RuntimeError('failed to create user')
        row = conn.execute('SELECT id FROM users WHERE username=?', [safe_user]).fetchone()
        if row is None:
            raise RuntimeError('failed to create user')
        return int(row['id'])

    return int(config.db.write_transaction(_tx))

def _bootstrap_super_admin_with_password_verifier(username: str, verifier_hex: str, salt_hex: str, iterations: int) -> int:
    safe_user = _normalize_username_required(username)
    safe_verifier = _normalize_password_verifier_hex(verifier_hex)
    safe_salt = _normalize_password_salt_hex(salt_hex)
    safe_iters = _normalize_password_iters(iterations)
    now = now_iso()

    def _tx(conn: sqlite3.Connection) -> int:
        has_registered_user = conn.execute("SELECT 1 FROM users WHERE COALESCE(TRIM(password_hash), '') <> '' LIMIT 1").fetchone() is not None
        if has_registered_user:
            raise ValueError('setup already completed')
        existing = conn.execute("SELECT id,password_hash FROM users WHERE username=?", [safe_user]).fetchone()
        if existing is None:
            try:
                conn.execute(
                    """
                    INSERT INTO users(
                        username,password_hash,password_salt,password_iters,password_updated_at,created_at,is_system_admin
                    )
                    VALUES(?,?,?,?,?,?,1)
                    """,
                    [safe_user, safe_verifier, safe_salt, safe_iters, now, now],
                )
            except sqlite3.IntegrityError as exc:
                msg = str(exc or '').strip().lower()
                if 'users.username' in msg:
                    raise ValueError('setup failed; username is unavailable') from exc
                raise
            existing = conn.execute("SELECT id,password_hash FROM users WHERE username=?", [safe_user]).fetchone()
            if existing is None:
                raise RuntimeError('failed to create super admin')
        else:
            current_hash = str(existing['password_hash'] or '').strip()
            if current_hash:
                raise ValueError('setup failed; username is unavailable')
            conn.execute(
                """
                UPDATE users
                SET password_hash=?,password_salt=?,password_iters=?,password_updated_at=?,is_system_admin=1
                WHERE id=?
                """,
                [safe_verifier, safe_salt, safe_iters, now, int(existing['id'])],
            )
        user_id = int(existing['id'])
        conn.execute("UPDATE users SET is_system_admin=0 WHERE id<>?", [user_id])
        return user_id

    return int(config.db.write_transaction(_tx))

def _create_session_for_user(user_id: int) -> str:
    uid = int(user_id)
    expires = (_utc_now() + timedelta(seconds=_C.AUTH_COOKIE_MAX_AGE)).isoformat()
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = sha256_hex_text(token)
        sid = f's-{secrets.token_hex(12)}'
        try:
            config.db.execute('INSERT INTO auth_sessions(id,user_id,token_hash,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,NULL)', [sid, uid, token_hash, now_iso(), expires])
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError('failed to create auth session')

def _create_sudo_session_for_user(user_id: int, scope: str) -> str:
    uid = int(user_id)
    safe_scope = str(scope or '').strip().lower()
    if not safe_scope:
        raise ValueError('invalid sudo scope')
    expires = (_utc_now() + timedelta(seconds=int(_C.SUDO_COOKIE_MAX_AGE))).isoformat()
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        token_hash = sha256_hex_text(token)
        sid = f'sudo-{secrets.token_hex(12)}'
        try:
            config.db.execute(
                'INSERT INTO sudo_sessions(id,user_id,scope,token_hash,created_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)',
                [sid, uid, safe_scope, token_hash, now_iso(), expires],
            )
            return token
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError('failed to create sudo session')

def _revoke_session_token(token: str) -> None:
    raw = str(token or '').strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return
    token_hash = sha256_hex_text(raw)
    config.db.execute('UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', [now_iso(), token_hash])

def _revoke_sudo_session_token(token: str) -> None:
    raw = str(token or '').strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return
    token_hash = sha256_hex_text(raw)
    config.db.execute('UPDATE sudo_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', [now_iso(), token_hash])

def _revoke_sudo_sessions_for_user(user_id: int) -> None:
    config.db.execute('UPDATE sudo_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', [now_iso(), int(user_id)])

def _session_identity(request: Request) -> dict | None:
    raw = str(request.cookies.get(_C.AUTH_COOKIE_NAME, '')).strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return None
    token_hash = sha256_hex_text(raw)
    row = config.db.fetch_one('\n        SELECT s.id AS session_id,s.user_id,s.expires_at,u.username\n        FROM auth_sessions s\n        JOIN users u ON u.id=s.user_id\n        WHERE s.token_hash=? AND s.revoked_at IS NULL\n        ', [token_hash])
    if row is None:
        return None
    expires_at = _parse_iso_utc(str(row['expires_at'] or ''))
    if expires_at is None or expires_at <= _utc_now():
        _revoke_session_token(raw)
        return None
    return {'session_id': row['session_id'], 'user_id': int(row['user_id']), 'username': str(row['username']), 'token': raw}

def _sudo_identity(request: Request, scope: str) -> dict | None:
    safe_scope = str(scope or '').strip().lower()
    if not safe_scope:
        return None
    raw = str(request.cookies.get(_C.SUDO_COOKIE_NAME, '')).strip()
    if not raw or not _C.SESSION_TOKEN_RE.fullmatch(raw):
        return None
    token_hash = sha256_hex_text(raw)
    row = config.db.fetch_one(
        '\n        SELECT s.id AS sudo_session_id,s.user_id,s.scope,s.expires_at\n        FROM sudo_sessions s\n        WHERE s.token_hash=? AND s.revoked_at IS NULL\n        ',
        [token_hash],
    )
    if row is None:
        return None
    row_scope = str(row['scope'] or '').strip().lower()
    if row_scope != safe_scope:
        _revoke_sudo_session_token(raw)
        return None
    expires_at = _parse_iso_utc(str(row['expires_at'] or ''))
    if expires_at is None or expires_at <= _utc_now():
        _revoke_sudo_session_token(raw)
        return None
    return {'sudo_session_id': str(row['sudo_session_id']), 'user_id': int(row['user_id']), 'scope': row_scope, 'token': raw}

def _has_sudo_session(request: Request, *, user_id: int, scope: str) -> bool:
    identity = _sudo_identity(request, scope)
    if identity is None:
        return False
    return int(identity['user_id']) == int(user_id)

def _session_user(request: Request) -> str:
    identity = _session_identity(request)
    if identity is None:
        return ''
    return str(identity['username'])

def _safe_next_path(raw: str | None, fallback: str='/') -> str:
    candidate = str(raw or '').strip()
    if not candidate:
        return fallback
    if not candidate.startswith('/') or candidate.startswith('//'):
        return fallback
    return candidate

def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += f'?{request.url.query}'
    if not _has_registered_users():
        return _redirect_response(f'/setup?next={quote_plus(target)}', status_code=303)
    return _redirect_response(f'/login?next={quote_plus(target)}', status_code=303)

def _request_origin_value(raw: str) -> str:
    value = str(raw or '').strip()
    if not value:
        return ''
    parsed = urlparse(value)
    scheme = str(parsed.scheme or '').strip().lower()
    netloc = str(parsed.netloc or '').strip().lower()
    if not scheme or not netloc:
        return ''
    return f'{scheme}://{netloc}'

def _expected_request_origin(request: Request) -> str:
    return f'{str(request.url.scheme).strip().lower()}://{str(request.url.netloc).strip().lower()}'

def _enforce_same_origin_state_change(request: Request | None) -> None:
    if request is None:
        return
    method = str(request.method or '').strip().upper()
    if method in {'GET', 'HEAD', 'OPTIONS', 'TRACE'}:
        return
    expected = _expected_request_origin(request)
    origin = _request_origin_value(str(request.headers.get('origin') or ''))
    if origin:
        if origin != expected:
            raise HTTPException(status_code=403, detail='cross-site request blocked')
        return
    referer = _request_origin_value(str(request.headers.get('referer') or ''))
    if referer:
        if referer != expected:
            raise HTTPException(status_code=403, detail='cross-site request blocked')
        return
    raise HTTPException(status_code=403, detail='missing origin/referrer for state-changing request')

def _login_rate_limit_key(username: str, request: Request | None) -> str:
    safe_user = str(username or '').strip().lower()
    ip = ''
    if request is not None:
        forwarded = str(request.headers.get('x-forwarded-for') or '').strip()
        if forwarded:
            ip = str(forwarded.split(',', 1)[0]).strip()
        if not ip:
            client = request.client
            ip = str(client.host).strip() if client is not None and client.host else ''
    if not ip:
        ip = 'unknown'
    return f'{ip}|{safe_user}'

def _login_rate_limit_check(key: str) -> None:
    now_monotonic = time.monotonic()
    with config.login_rate_limit_lock:
        state = config.login_rate_limit_state.get(key)
        if state is None:
            return
        blocked_until = float(state.get('blocked_until') or 0.0)
        if blocked_until > now_monotonic:
            wait_sec = max(1, int(round(blocked_until - now_monotonic)))
            raise ValueError(f'too many failed attempts; retry in {wait_sec}s')
        window_start = float(state.get('window_start') or 0.0)
        if window_start <= 0.0 or now_monotonic - window_start > _C.LOGIN_RATE_LIMIT_WINDOW_SEC:
            config.login_rate_limit_state.pop(key, None)

def _login_rate_limit_fail(key: str) -> None:
    now_monotonic = time.monotonic()
    with config.login_rate_limit_lock:
        state = config.login_rate_limit_state.get(key)
        if state is None:
            state = {'window_start': now_monotonic, 'failures': 0, 'blocked_until': 0.0}
        window_start = float(state.get('window_start') or 0.0)
        if window_start <= 0.0 or now_monotonic - window_start > _C.LOGIN_RATE_LIMIT_WINDOW_SEC:
            state = {'window_start': now_monotonic, 'failures': 0, 'blocked_until': 0.0}
        failures = int(state.get('failures') or 0) + 1
        state['failures'] = failures
        if failures >= _C.LOGIN_RATE_LIMIT_MAX_FAILURES:
            state['blocked_until'] = now_monotonic + _C.LOGIN_RATE_LIMIT_BLOCK_SEC
            state['window_start'] = now_monotonic
            state['failures'] = 0
        config.login_rate_limit_state[key] = state

def _login_rate_limit_success(key: str) -> None:
    with config.login_rate_limit_lock:
        config.login_rate_limit_state.pop(key, None)

async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith('/static/') or path in {'/login', '/register', '/setup'}:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    protected = (
        path == '/'
        or path in {'/problems', '/contests'}
        or path.startswith('/problems/')
        or path.startswith('/contests/')
        or path.startswith('/switch-')
        or path.startswith('/sudo')
        or (path == '/logout')
    )
    if not protected:
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    user = _session_user(request)
    if not user:
        response = _login_redirect(request)
        _apply_security_headers(response)
        return response
    _enforce_same_origin_state_change(request)
    if _C.ROOT_PROBLEMS_PATH_RE.fullmatch(path) or _C.ROOT_CONTESTS_PATH_RE.fullmatch(path):
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    sm = _C.SETTINGS_USER_PATH_RE.match(path)
    if sm:
        if sm.group('user') != user:
            rest = sm.group('rest') or ''
            target = f'/problems/{user}/settings{rest}'
            if request.url.query:
                target += f'?{request.url.query}'
            return _redirect_response(target, status_code=303)
        response = await call_next(request)
        _apply_security_headers(response)
        return response
    pm = _C.PROBLEM_USER_PATH_RE.match(path)
    if pm and pm.group('user') != user:
        rest = pm.group('rest') or ''
        target = f"/problems/{pm.group('problem')}/{user}{rest}"
        if request.url.query:
            target += f'?{request.url.query}'
        return _redirect_response(target, status_code=303)
    cm = _C.CONTEST_USER_PATH_RE.match(path)
    if cm and cm.group('user') != user:
        rest = cm.group('rest') or ''
        target = f"/contests/{cm.group('contest')}/{user}{rest}"
        if request.url.query:
            target += f'?{request.url.query}'
        return _redirect_response(target, status_code=303)
    response = await call_next(request)
    _apply_security_headers(response)
    return response

