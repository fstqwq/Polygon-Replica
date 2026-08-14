from datetime import datetime, timezone

from app.db import now_iso
from app.service.judgehost.batch.model import HostLeaseRelease
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.judgehost.configuration import JudgehostSettings
from app.service.judgehost.host.registry import JudgehostHostRegistry
from app.service.judgehost.task.registry import JudgehostTaskRegistry
from app.service.judgehost.task.time import parse_iso_utc
from app.service.judgehost.validation import normalize_judgehost_hostname


class JudgehostHostStatus:
    """Project host status and enablement from the canonical host owner."""

    def __init__(
        self,
        hosts: JudgehostHostRegistry,
        tasks: JudgehostTaskRegistry,
        batch_runtime: JudgehostBatchRuntime,
    ) -> None:
        self._hosts = hosts
        self._tasks = tasks
        self._batch_runtime = batch_runtime

    def record_peer(self, hostname: str, peer_addr: str) -> None:
        safe_host = normalize_judgehost_hostname(hostname)
        safe_peer = peer_addr.strip()
        if safe_peer:
            self._hosts.record_peer(safe_host, safe_peer)

    def set_enabled(self, hostname: str, enabled: bool) -> HostLeaseRelease:
        safe_host = normalize_judgehost_hostname(hostname)
        self._hosts.set_enabled(safe_host, enabled)
        if enabled:
            return HostLeaseRelease(0, 0, (), (), ())
        return self._batch_runtime.release_host_leases(safe_host, now_text=now_iso())

    def status(self, settings: JudgehostSettings) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        active_by_host = self._batch_runtime.active_lease_counts()
        runtime_telemetry = self._batch_runtime.host_telemetry_snapshot()
        toolchains = {
            hostname: [item.status_payload() for _key, item in sorted(rows.items())]
            for hostname, rows in self._hosts.toolchain_rows().items()
        }
        host_rows = sorted(
            self._hosts.host_rows(),
            key=lambda row: (row["last_seen_at"], row["hostname"]),
            reverse=True,
        )
        rows: list[dict[str, object]] = []
        online_count = 0
        for row in host_rows:
            hostname = row["hostname"]
            seen_at = parse_iso_utc(row.get("last_seen_at"))
            age_sec: int | None = None
            online = False
            if seen_at is not None:
                age = max(0.0, (now - seen_at).total_seconds())
                age_sec = int(age)
                online = age <= settings.online_window_sec
            if online and row["enabled"]:
                online_count += 1
            telemetry = runtime_telemetry.get(hostname)
            rows.append(
                {
                    "hostname": hostname,
                    "peer_addr": row.get("peer_addr", ""),
                    "enabled": row["enabled"],
                    "online": online,
                    "age_sec": age_sec,
                    "last_seen_at": row["last_seen_at"],
                    "first_seen_at": row["first_seen_at"],
                    "last_action": row["last_action"],
                    "last_task_id": row["last_task_id"],
                    "last_run_id": row["last_run_id"],
                    "toolchains": toolchains.get(hostname, []),
                    "active_leases": int(active_by_host.get(hostname, 0)),
                    "update_count": row["update_count"],
                    "judged_case_count": 0 if telemetry is None else telemetry["judged_case_count"],
                    "last_judging_at": None if telemetry is None else telemetry["last_judging_at"],
                    "last_judging": None if telemetry is None else telemetry["last_judging"],
                    "recent_avg_per_case_sec": (
                        None if telemetry is None else telemetry["recent_avg_per_case_sec"]
                    ),
                }
            )
        counts = self._tasks.status_counts()
        return {
            "enabled": settings.enabled,
            "auth_configured": bool(settings.api_token),
            "hosts_total": len(rows),
            "hosts_online": online_count,
            "hosts": rows,
            "queue": {
                "queued": counts["queued"],
                "leased": counts["leased"],
                "completed": counts["completed"],
                "failed": counts["failed"],
            },
        }
