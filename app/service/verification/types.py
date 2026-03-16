from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    VERIFICATION = "verification"


class Status(StrEnum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE = frozenset((Status.QUEUED.value, Status.PENDING.value, Status.RUNNING.value))
FAILED = frozenset((Status.FAILED.value, Status.CANCELLED.value))
