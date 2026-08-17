import base64
import binascii
import json
import re
from collections.abc import Mapping
from pathlib import Path
from app.service.judgehost.domjudge.limits import (
    compile_output_kb,
    config_int,
    upload_max_bytes,
)
from app.service.judgehost.languages import JUDGEHOST_LANGUAGES

_CONTEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def decode_json_object(raw: object) -> dict[str, object]:
    text = decode_text(raw=raw)
    if not text:
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise RuntimeError("DOMjudge JSON value must be an object")
    result: dict[str, object] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise RuntimeError("DOMjudge JSON object keys must be strings")
        result[key] = value
    return result


def decode_text(
    *,
    raw: object,
    default: str = "",
    lower: bool = False,
) -> str:
    """Decode one optional textual field at an untyped protocol boundary."""
    if raw is None:
        return default
    if not isinstance(raw, str):
        raise RuntimeError("DOMjudge text field must be a string")
    text = raw.strip()
    if text:
        return text.lower() if lower else text
    return default


def decode_basename(*, raw: object, default: str = "") -> str:
    token = decode_text(raw=raw, default=default)
    if not token:
        return default
    return Path(token).name


def decode_contest_id(raw: object) -> str:
    token = decode_text(raw=raw)
    return token if _CONTEST_ID_RE.fullmatch(token) else "local"


def decode_base64(text: str | bytes | bytearray | memoryview | None) -> bytes:
    if text is None:
        return b""
    if isinstance(text, str):
        raw = text.strip()
    else:
        try:
            raw = bytes(text).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("DOMjudge payload must be base64 ASCII text") from exc
    if not raw:
        return b""
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("DOMjudge payload is not valid base64") from exc


def config_payload(values: Mapping[str, object]) -> dict[str, object]:
    compile_timeout = config_int(values, "TOOLCHAIN_COMPILE_TIMEOUT_SEC")
    compile_mem_mb = config_int(values, "TOOLCHAIN_COMPILE_MEMORY_MB")
    return {
        "diskspace_error": 1048576,
        # DOMjudge applies output_storage_limit to program stdout artifacts.
        # Generator stdout becomes verification input for downstream tasks, so
        # this must follow the run input/output cap, not the saved-log cap.
        "output_storage_limit": upload_max_bytes(values),
        "script_timelimit": compile_timeout,
        "script_memory_limit": int(compile_mem_mb * 1024),
        "script_filesize_limit": int(compile_output_kb(values)),
        "timelimit_overshoot": "1s|100%",
    }


def languages_payload() -> list[dict[str, object]]:
    return [
        {
            "id": language.language_id,
            "extensions": list(language.extensions),
        }
        for language in JUDGEHOST_LANGUAGES
    ]
