from dataclasses import dataclass
from typing import NotRequired, TypedDict


def judgehost_name_sort_key(hostname: str) -> tuple[str, str]:
    return (hostname.casefold(), hostname)


class JudgehostHostRow(TypedDict):
    hostname: str
    enabled: bool
    first_seen_at: str
    last_seen_at: str
    peer_addr: NotRequired[str]


@dataclass(frozen=True, slots=True)
class HostToolchainTelemetry:
    language_id: str
    compiler: str
    runner: str
    observed_at: str
    judgetask_id: int

    def status_payload(self) -> dict[str, object]:
        return {
            "language_id": self.language_id,
            "compiler": self.compiler,
            "runner": self.runner,
            "observed_at": self.observed_at,
            "judgetask_id": self.judgetask_id,
        }
