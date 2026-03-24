from __future__ import annotations

import re

ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


def is_canonical_artifact_id(value: str) -> bool:
    return bool(ARTIFACT_ID_RE.fullmatch(str(value or "")))
