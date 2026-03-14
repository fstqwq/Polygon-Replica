from __future__ import annotations

import os
import time

from app.db import DB


def effective_compile_jobs(configured: object, target_count: int) -> int:
    auto_jobs = max(1, min(4, os.cpu_count() or 1))
    try:
        requested = int(configured)
    except Exception:
        requested = 0
    bounded = auto_jobs if requested <= 0 else max(1, min(16, requested))
    return max(1, min(bounded, max(1, target_count)))


def wait_build_terminal_status(
    db: DB,
    *,
    verification_id: str,
    timeout_sec: float,
    poll_sec: float,
) -> str:
    safe_verification_id = str(verification_id or "").strip()
    if not safe_verification_id:
        return ""
    deadline = time.monotonic() + max(0.5, float(timeout_sec))
    while time.monotonic() < deadline:
        row = db.fetch_one("SELECT status FROM verifications WHERE id=?", [safe_verification_id])
        status = str(row["status"] or "").strip().lower() if row is not None else ""
        if status in {"ok", "failed", "cancelled"}:
            return status
        time.sleep(max(0.01, float(poll_sec)))
    return ""


