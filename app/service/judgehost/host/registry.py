import threading

from app.db import now_iso
from app.service.judgehost.host.model import (
    HostToolchainTelemetry,
    JudgehostHostRow,
)


class JudgehostHostRegistry:
    """Own process-local Judgehost identity, status, and toolchain telemetry."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._hosts: dict[str, JudgehostHostRow] = {}
        self._toolchains: dict[str, dict[str, HostToolchainTelemetry]] = {}

    def record_contact(self, *, hostname: str) -> None:
        timestamp = now_iso()
        with self._lock:
            self._record_contact_locked(
                hostname=hostname,
                timestamp=timestamp,
            )

    def _record_contact_locked(
        self,
        *,
        hostname: str,
        timestamp: str,
    ) -> None:
        row = self._hosts.get(hostname)
        if row is None:
            self._hosts[hostname] = {
                "hostname": hostname,
                "enabled": True,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
            }
            return
        row["last_seen_at"] = timestamp

    def record_peer(self, hostname: str, peer_addr: str) -> None:
        with self._lock:
            self._record_contact_locked(
                hostname=hostname,
                timestamp=now_iso(),
            )
            self._hosts[hostname]["peer_addr"] = peer_addr

    def enabled(self, hostname: str) -> bool:
        with self._lock:
            row = self._hosts.get(hostname)
            return True if row is None else row["enabled"]

    def set_enabled(self, hostname: str, enabled: bool) -> None:
        with self._lock:
            row = self._hosts.get(hostname)
            if row is None:
                raise ValueError(f"unknown judgehost: {hostname}")
            row["enabled"] = enabled

    def record_toolchain(self, hostname: str, telemetry: HostToolchainTelemetry) -> None:
        with self._lock:
            self._toolchains.setdefault(hostname, {})[telemetry.language_id] = telemetry

    def host_rows(self) -> list[JudgehostHostRow]:
        with self._lock:
            return [row.copy() for row in self._hosts.values()]

    def toolchain_rows(self) -> dict[str, dict[str, HostToolchainTelemetry]]:
        with self._lock:
            return {
                hostname: dict(entries)
                for hostname, entries in self._toolchains.items()
            }

    def clear(self) -> None:
        with self._lock:
            self._hosts.clear()
            self._toolchains.clear()
