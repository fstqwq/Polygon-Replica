import re
import secrets


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def canonical_run_id(raw: str) -> str:
    if type(raw) is not str or _RUN_ID_RE.fullmatch(raw) is None:
        raise ValueError("invalid execution run id")
    return raw


def new_run_id() -> str:
    return canonical_run_id(f"r-{secrets.token_hex(6)}")
