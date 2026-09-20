from dataclasses import dataclass
from typing import NotRequired, TypedDict

from app.service.judgehost.batch.model import LastJudgingRow


def judgehost_name_sort_key(hostname: str) -> tuple[str, str]:
    return (hostname.casefold(), hostname)


class JudgehostHostRow(TypedDict):
    hostname: str
    enabled: bool
    first_seen_at: str
    last_seen_at: str
    peer_addr: NotRequired[str]


class HostToolchainStatus(TypedDict):
    language_id: str
    compiler: str
    runner: str
    observed_at: str
    judgetask_id: int


class JudgehostStatusRow(TypedDict):
    hostname: str
    peer_addr: str
    enabled: bool
    online: bool
    age_sec: int | None
    first_seen_at: str
    last_seen_at: str
    toolchains: list[HostToolchainStatus]
    active_leases: int
    judged_case_count: int
    last_judging_at: str | None
    last_judging: LastJudgingRow | None
    recent_avg_per_case_sec: float | None


class JudgehostQueueStatus(TypedDict):
    queued: int
    leased: int
    completed: int
    failed: int


class JudgehostStatus(TypedDict):
    enabled: bool
    auth_configured: bool
    hosts_total: int
    hosts_online: int
    hosts: list[JudgehostStatusRow]
    queue: JudgehostQueueStatus


@dataclass(frozen=True, slots=True)
class HostToolchainTelemetry:
    language_id: str
    compiler: str
    runner: str
    observed_at: str
    judgetask_id: int

    def status_payload(self) -> HostToolchainStatus:
        return {
            "language_id": self.language_id,
            "compiler": self.compiler,
            "runner": self.runner,
            "observed_at": self.observed_at,
            "judgetask_id": self.judgetask_id,
        }
