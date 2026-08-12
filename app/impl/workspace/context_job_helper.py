from __future__ import annotations

from app.impl.runtime.dependency import runtime


def allocate_verification_id() -> str:
    return runtime().verification_service.allocate_verification_id()
