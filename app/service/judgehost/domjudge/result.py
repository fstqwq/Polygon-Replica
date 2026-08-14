from typing import Callable

from app.service.platform.error_text import bounded_display_text


def parse_nonnegative_float(raw: object, default: float = 0.0) -> float:
    try:
        value = float(str(raw or "").strip())
    except Exception:
        return float(default)
    if value < 0:
        return float(default)
    return value


def parse_int(raw: object, default: int = 0) -> int:
    if isinstance(raw, bool):
        return int(raw)
    if not isinstance(raw, (int, str, bytes, bytearray)):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _run_time_limit_sec(run_cfg_obj: dict[str, object]) -> float:
    cfg = run_cfg_obj
    tl_sec = parse_nonnegative_float(cfg.get("time_limit"), 0.0)
    if tl_sec > 0:
        return float(tl_sec)
    tl_ms = parse_int(cfg.get("time_limit_ms"), 0)
    if tl_ms > 0:
        return float(max(0.0, float(tl_ms) / 1000.0))
    return 0.0


def rewrite_untrusted_runresult(
    runresult: str,
    *,
    cpu_sec: float,
    run_cfg_obj: dict[str, object],
) -> str:
    token = str(runresult or "").strip().lower()
    if token not in {"wrong-answer", "run-error", "no-output"}:
        return token
    tl_sec = _run_time_limit_sec(run_cfg_obj)
    if tl_sec <= 0:
        return token
    cpu_total_sec = parse_nonnegative_float(cpu_sec, 0.0)
    if cpu_total_sec <= tl_sec:
        return token
    return "timelimit"


def parse_metadata(text: str) -> dict[str, str]:
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


def parse_bool(raw: object, default: bool = False) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def bounded_feedback_text(text: str, *, limit_bytes: int) -> str:
    return bounded_display_text(
        str(text or ""),
        limit_bytes=limit_bytes,
    )


def bounded_feedback_bytes(blob: bytes, *, limit_bytes: int) -> str:
    return bounded_feedback_text(
        bytes(blob or b"").decode("utf-8", errors="replace"),
        limit_bytes=limit_bytes,
    )


def _feedback_token_order(
    *,
    runresult: str,
    output_error_ref: str,
    output_diff_ref: str,
    team_message_ref: str,
) -> list[str]:
    runresult_token = str(runresult or "").strip().lower()
    if runresult_token in {"run-error", "internal-error"}:
        ordered = [output_error_ref, output_diff_ref, team_message_ref]
    else:
        ordered = [output_diff_ref, team_message_ref, output_error_ref]
    feedback_tokens: list[str] = []
    for token in ordered:
        feedback_token = str(token or "").strip()
        if feedback_token:
            feedback_tokens.append(feedback_token)
    return feedback_tokens


def feedback_text_and_files(
    *,
    read_blob: Callable[[str], bytes | None],
    runresult: str,
    output_error_ref: str,
    output_diff_ref: str,
    team_message_ref: str,
    limit_bytes: int,
) -> tuple[str, list[str]]:
    feedback_files = _feedback_token_order(
        runresult=runresult,
        output_error_ref=output_error_ref,
        output_diff_ref=output_diff_ref,
        team_message_ref=team_message_ref,
    )
    feedback_text = ""
    for token in feedback_files:
        if feedback_text:
            break
        blob = read_blob(token)
        if blob is not None:
            feedback_text = bounded_feedback_bytes(
                blob,
                limit_bytes=limit_bytes,
            )
    return feedback_text, feedback_files


def verdict_from_runresult(runresult: str) -> str:
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
