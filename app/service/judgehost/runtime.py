from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.service.platform.error_text import bounded_display_text


def now_iso_after(seconds: float) -> str:
    sec = max(0.0, float(seconds))
    return (datetime.now(timezone.utc) + timedelta(seconds=sec)).isoformat()


def parse_iso_utc(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def domjudge_parse_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(str(raw or "").strip())
    except Exception:
        return float(default)
    if value < 0:
        return float(default)
    return value


def domjudge_parse_int(raw: object, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def domjudge_run_time_limit_sec(run_cfg_obj: dict[str, object]) -> float:
    cfg = run_cfg_obj
    tl_sec = domjudge_parse_float(cfg.get("time_limit"), 0.0)
    if tl_sec > 0:
        return float(tl_sec)
    tl_ms = domjudge_parse_int(cfg.get("time_limit_ms"), 0)
    if tl_ms > 0:
        return float(max(0.0, float(tl_ms) / 1000.0))
    return 0.0


def domjudge_rewrite_untrusted_runresult(
    runresult: str,
    *,
    cpu_sec: float,
    run_cfg_obj: dict[str, object],
) -> str:
    token = str(runresult or "").strip().lower()
    if token not in {"wrong-answer", "run-error", "no-output"}:
        return token
    tl_sec = domjudge_run_time_limit_sec(run_cfg_obj)
    if tl_sec <= 0:
        return token
    cpu_total_sec = domjudge_parse_float(cpu_sec, 0.0)
    if cpu_total_sec <= tl_sec:
        return token
    return "timelimit"


def domjudge_parse_meta_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        token = str(line or "").strip()
        if not token or ":" not in token:
            continue
        key, value = token.split(":", 1)
        safe_key = str(key or "").strip().lower()
        if not safe_key:
            continue
        out[safe_key] = str(value or "").strip()
    return out


def domjudge_bool(raw: object, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def domjudge_feedback_text_from_text(text: str) -> str:
    return bounded_display_text(str(text or ""))


def domjudge_feedback_text_from_bytes(blob: bytes) -> str:
    return domjudge_feedback_text_from_text(
        bytes(blob or b"").decode("utf-8", errors="replace"),
    )


def domjudge_verdict_from_runresult(runresult: str) -> str:
    token = str(runresult or "").strip().lower()
    mapping = {
        "correct": "OK",
        "compiler-error": "CE",
        "timelimit": "TL",
        "run-error": "RE",
        "wrong-answer": "WA",
        "no-output": "WA",
        "checker-fail": "FL",
        "output-limit": "FL",
        "compare-error": "FL",
        "internal-error": "FL",
    }
    return mapping.get(token, "FL")
