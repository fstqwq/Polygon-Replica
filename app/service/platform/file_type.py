from pathlib import Path

_BINARY_SNIFF_BYTES = 8192


def looks_like_binary_file(path: Path, sniff_bytes: int = _BINARY_SNIFF_BYTES) -> bool:
    cap = max(1, int(sniff_bytes))
    try:
        with path.open("rb") as fh:
            chunk = fh.read(cap)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
