import re
import secrets


_VERIFICATION_ID_RE = re.compile(r"^ver-([0-9a-f]+)$")
_MAX_NUMERIC_ID = (1 << 63) - 1


def canonical_verification_id(raw: str) -> str:
    match = _VERIFICATION_ID_RE.fullmatch(raw)
    if match is None:
        raise RuntimeError("invalid verification id")
    numeric_id = int(match.group(1), 16)
    if numeric_id <= 0 or numeric_id > _MAX_NUMERIC_ID:
        raise RuntimeError("invalid verification id")
    if raw != f"ver-{numeric_id:x}":
        raise RuntimeError("invalid verification id")
    return raw


def new_verification_id() -> str:
    return f"ver-{secrets.randbelow(_MAX_NUMERIC_ID) + 1:x}"
