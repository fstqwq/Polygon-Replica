from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


_DOMJUDGE_INTERNAL_BUILD_PREFIX_RE = re.compile(
    r"/opt/domjudge/judgehost/judgings/[^:\s]+/endpoint-[^:\s]+/executable/[^:\s]+/[^:\s]+/build/"
)


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


def _domjudge_compact_feedback_line(text: str) -> str:
    normalized = _DOMJUDGE_INTERNAL_BUILD_PREFIX_RE.sub("", text.replace("\\", "/"))
    return " ".join(normalized.split())


def _domjudge_feedback_line_score(line: str) -> int:
    token = line.lower()
    if (
        " error:" in token
        or token.startswith("error:")
        or " fatal error:" in token
        or token.startswith("fatal error:")
    ):
        return 100
    if (
        "undefined reference" in token
        or token.startswith("collect2:")
        or " ld returned " in token
        or "no such file or directory" in token
    ):
        return 90
    if " warning:" in token or token.startswith("warning:"):
        return 70
    if "compiling failed with exitcode" in token or "compiler output:" in token:
        return 40
    if " in function " in token or token.startswith("in file included from "):
        return 5
    return 10


def domjudge_feedback_line_from_text(text: str, *, max_chars: int = 240) -> str:
    first_line = ""
    best_line = ""
    best_score = -1
    for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _domjudge_compact_feedback_line(raw_line)
        if not line:
            continue
        if not first_line:
            first_line = line
        score = _domjudge_feedback_line_score(line)
        if score > best_score:
            best_line = line
            best_score = score
    selected = best_line or first_line
    if len(selected) <= max_chars:
        return selected
    return selected[:max_chars].rstrip() + "..."


def domjudge_feedback_line_from_bytes(blob: bytes, *, max_chars: int = 240) -> str:
    return domjudge_feedback_line_from_text(
        bytes(blob or b"").decode("utf-8", errors="replace"),
        max_chars=max_chars,
    )


