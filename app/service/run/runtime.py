from __future__ import annotations

import re

from app.service.verification.runtime import (
    coerce_int,
    effective_run_timeout_ms,
    normalize_pass_limit,
    normalize_problem_mode,
    normalize_time_limit_ms,
    wall_time_slack_sec_for_mode,
)

RUN_TEST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.in$")

# Explicit re-exports for downstream consumers.
__all__ = [
    "RUN_TEST_NAME_RE",
    "coerce_int",
    "effective_run_timeout_ms",
    "normalize_pass_limit",
    "normalize_problem_mode",
    "normalize_time_limit_ms",
    "wall_time_slack_sec_for_mode",
]
