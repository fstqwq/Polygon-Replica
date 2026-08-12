from __future__ import annotations

from enum import StrEnum
from typing import TypedDict


class Kind(StrEnum):
    ALL = "all"
    SAMPLE = "sample"
    CUSTOM = "custom"


class VerificationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationTaskStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    LEASED = "leased"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE = frozenset(
    (VerificationStatus.QUEUED.value, VerificationStatus.RUNNING.value)
)


class WorkspaceVerificationRow(TypedDict):
    id: str
    status: VerificationStatus
    signature: str
    source_commit: str
    kind: str
    fail_reason: str
    error: str
    sanity_status: str
    created_at: str
    finished_at: str


WorkspaceVerificationKey = tuple[int, int]
