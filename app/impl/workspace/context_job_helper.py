from __future__ import annotations

import secrets

from app.impl.runtime.config import config


def allocate_run_id() -> str:
    return f"r-{secrets.token_hex(6)}"


def allocate_verification_id() -> str:
    return config.verification_service.allocate_verification_id()
