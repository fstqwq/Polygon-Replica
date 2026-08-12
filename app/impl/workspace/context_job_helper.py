from __future__ import annotations

from app.impl.runtime.config import config


def allocate_verification_id() -> str:
    return config.verification_service.allocate_verification_id()
