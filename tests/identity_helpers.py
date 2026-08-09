from __future__ import annotations

import hashlib


def canonical_test_verification_id(label: str) -> str:
    digest = int(hashlib.sha256(label.encode("utf-8")).hexdigest(), 16)
    numeric_id = digest & ((1 << 63) - 1)
    return f"ver-{numeric_id or 1:x}"
