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

    def record_event(
        self,
        *,
        hostname: str,
        action: str,
        task_id: str = "",
        run_id: str = "",
    ) -> None:
        timestamp = now_iso()
        with self._lock:
            self._record_event_locked(
                hostname=hostname,
                action=action,
                task_id=task_id,
                run_id=run_id,
                timestamp=timestamp,
            )

    def _record_event_locked(
        self,
        *,
        hostname: str,
        action: str,
        task_id: str,
        run_id: str,
        timestamp: str,
    ) -> None:
        row = self._hosts.get(hostname)
        if row is None:
            self._hosts[hostname] = {
                "hostname": hostname,
                "enabled": True,
                "first_seen_at": timestamp,
                "last_seen_at": timestamp,
                "last_action": action,
                "last_task_id": task_id,
                "last_run_id": run_id,
                "update_count": 1,
            }
            return
        row["last_seen_at"] = timestamp
        row["last_action"] = action
        row["last_task_id"] = task_id
        row["last_run_id"] = run_id
        row["update_count"] += 1

    def record_peer(self, hostname: str, peer_addr: str) -> None:
        with self._lock:
            self._record_event_locked(
                hostname=hostname,
                action="peer",
                task_id="",
                run_id="",
                timestamp=now_iso(),
            )
            self._hosts[hostname]["peer_addr"] = peer_addr

    def enabled(self, hostname: str) -> bool:
        with self._lock:
            row = self._hosts.get(hostname)
            return True if row is None else row["enabled"]

    def set_enabled(self, hostname: str, enabled: bool) -> None:
        with self._lock:
            self._record_event_locked(
                hostname=hostname,
                action="enabled" if enabled else "disabled",
                task_id="",
                run_id="",
                timestamp=now_iso(),
            )
            self._hosts[hostname]["enabled"] = enabled

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
