from __future__ import annotations

import os
from app.service.disk.verification_store import VerificationStore


def effective_compile_jobs(configured: object, target_count: int) -> int:
    auto_jobs = max(1, min(4, os.cpu_count() or 1))
    try:
        requested = int(configured)
    except Exception:
        requested = 0
    bounded = auto_jobs if requested <= 0 else max(1, min(16, requested))
    return max(1, min(bounded, max(1, target_count)))


def wait_build_terminal_status(
    store: VerificationStore,
    *,
    verification_id: str,
    timeout_sec: float,
    poll_sec: float,
) -> str:
    return store.wait_terminal_status(
        verification_id,
        timeout_sec=float(timeout_sec),
        poll_sec=float(poll_sec),
    )


