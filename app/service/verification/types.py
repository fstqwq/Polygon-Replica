from __future__ import annotations

from enum import StrEnum
from typing import TypedDict


class Kind(StrEnum):
    ALL = "all"
    SAMPLE = "sample"
    CUSTOM = "custom"


class Status(StrEnum):
    QUEUED = "queued"
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


ACTIVE = frozenset((Status.QUEUED.value, Status.PENDING.value, Status.RUNNING.value))


class WorkspaceVerificationRow(TypedDict):
    id: str
    status: str
    signature: str
    source_commit: str
    kind: str
    fail_reason: str
    error: str
    sanity_status: str
    created_at: str
    finished_at: str


WorkspaceVerificationKey = tuple[int, int]

_CANCEL_REASONS = frozenset(
    (
        "verification cancelled",
        "verification cancelled by user",
        "cancelled on service startup",
    )
)


def is_cancel_reason(reason: str) -> bool:
    return reason in _CANCEL_REASONS
