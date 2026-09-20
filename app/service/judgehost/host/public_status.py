"""Safe public projection of host registry state."""

import re
import time
from pathlib import PurePosixPath
from typing import Callable, TypedDict

from app.service.judgehost.domjudge.compile_spec import CompileSpecStatus
from app.service.judgehost.host.model import (
    HostToolchainStatus,
    JudgehostStatus,
    JudgehostStatusRow,
)
from app.service.judgehost.languages import JUDGEHOST_LANGUAGE_BY_ID


_PRIVATE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s]+")


class PublicCompileSpec(TypedDict):
    language_id: str
    language_label: str
    command: str
    arguments: list[str]


class PublicToolchainVersion(TypedDict):
    compiler: str
    runner: str
    host_count: int


class PublicToolchainSummary(TypedDict):
    language_id: str
    language_label: str
    versions: list[PublicToolchainVersion]
    agrees: bool


class PublicJudgehostView(TypedDict):
    label: str
    state: str
    tone: str
    last_contact: str
    activity: str
    judged_cases: int
    recent_average: str


class PublicJudgehostStatus(TypedDict):
    enabled: bool
    hosts_online: int
    hosts_total: int
    queued: int
    busy_hosts: int
    summary: str
    tone: str
    hosts: list[PublicJudgehostView]
    compile_specs: list[PublicCompileSpec]
    toolchains: list[PublicToolchainSummary]
    toolchain_mismatch: bool


def _duration_label(age_sec: int | None) -> str:
    if age_sec is None or age_sec < 0:
        return "not reported"
    if age_sec < 60:
        return "just now"
    minutes = age_sec // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _safe_command(raw: str) -> str:
    token = raw.replace("\\", "/").strip()
    return PurePosixPath(token).name or "unknown"


def _safe_argument(raw: str) -> str:
    token = " ".join(raw.split())[:240]
    return _PRIVATE_PATH_RE.sub("[path]", token)


def _safe_version_lines(raw: str) -> tuple[str, str]:
    lines: list[str] = []
    for raw_line in raw.replace("\r", "\n").splitlines():
        line = " ".join(raw_line.split())
        if not line or line.startswith("command="):
            continue
        line = _PRIVATE_PATH_RE.sub("[path]", line)
        lines.append(line[:240])
    if not lines:
        return "", ""
    return lines[0], "\n".join(lines)


def _reported_toolchains(raw_toolchains: list[HostToolchainStatus]) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for raw in raw_toolchains:
        language_id = raw["language_id"]
        if language_id not in JUDGEHOST_LANGUAGE_BY_ID:
            continue
        _compiler_display, compiler_key = _safe_version_lines(raw["compiler"])
        _runner_display, runner_key = _safe_version_lines(raw["runner"])
        if compiler_key or runner_key:
            entries[language_id] = (compiler_key, runner_key)
    return entries


def _toolchain_summaries(
    online_hosts: list[JudgehostStatusRow],
) -> list[PublicToolchainSummary]:
    version_counts: dict[str, dict[tuple[str, str], int]] = {}
    for raw in online_hosts:
        for language_id, version in _reported_toolchains(raw["toolchains"]).items():
            language_counts = version_counts.setdefault(language_id, {})
            language_counts[version] = language_counts.get(version, 0) + 1
    summaries: list[PublicToolchainSummary] = []
    for language_id, language in JUDGEHOST_LANGUAGE_BY_ID.items():
        counts_for_language = version_counts.get(language_id)
        if not counts_for_language:
            continue
        versions: list[PublicToolchainVersion] = []
        for (compiler_raw, runner_raw), host_count in sorted(counts_for_language.items()):
            versions.append(
                {
                    "compiler": (compiler_raw.splitlines()[0] if compiler_raw else "not reported"),
                    "runner": runner_raw.splitlines()[0] if runner_raw else "",
                    "host_count": host_count,
                }
            )
        summaries.append(
            {
                "language_id": language_id,
                "language_label": language.label,
                "versions": versions,
                "agrees": len(versions) == 1,
            }
        )
    return summaries


def _public_hosts(
    hosts_source: list[JudgehostStatusRow],
) -> list[PublicJudgehostView]:
    hosts: list[PublicJudgehostView] = []
    for index, raw in enumerate(hosts_source, start=1):
        online = raw["enabled"] and raw["online"]
        active_leases = raw["active_leases"]
        state = "online" if online else "offline"
        recent_raw = raw["recent_avg_per_case_sec"]
        recent_average = (
            f"{recent_raw:.3f}s per case"
            if recent_raw is not None
            else "not available"
        )
        hosts.append(
            {
                "label": f"Judgehost {index}",
                "state": state,
                "tone": "ok" if online else "danger",
                "last_contact": _duration_label(raw["age_sec"]),
                "activity": "busy" if online and active_leases > 0 else "idle",
                "judged_cases": max(0, raw["judged_case_count"]),
                "recent_average": recent_average,
            }
        )
    return hosts


def _health_summary(
    enabled: bool,
    hosts_online: int,
    hosts_total: int,
    busy_hosts: int,
) -> tuple[str, str]:
    if not enabled:
        return "disabled", "muted"
    if hosts_online <= 0:
        return "offline", "danger"
    if hosts_online < hosts_total:
        return f"{hosts_online}/{hosts_total} online ({busy_hosts} busy)", "warn"
    return f"{hosts_online} online ({busy_hosts} busy)", "ok"


def _compile_specs(
    raw_compile_specs: list[CompileSpecStatus],
) -> list[PublicCompileSpec]:
    specs: list[PublicCompileSpec] = []
    for raw in raw_compile_specs:
        language_id = raw["language_id"]
        language = JUDGEHOST_LANGUAGE_BY_ID.get(language_id)
        if language is None:
            continue
        arguments = [_safe_argument(value) for value in raw["arguments"]]
        specs.append(
            {
                "language_id": language_id,
                "language_label": language.label,
                "command": _safe_command(raw["command"]),
                "arguments": arguments,
            }
        )
    return specs


def project_public_status(
    raw_status: JudgehostStatus,
    raw_compile_specs: list[CompileSpecStatus],
) -> PublicJudgehostStatus:
    hosts_source = raw_status["hosts"]
    online_hosts = [
        raw for raw in hosts_source if raw["enabled"] and raw["online"]
    ]
    toolchains = _toolchain_summaries(online_hosts)
    public_hosts = _public_hosts(hosts_source)

    enabled = raw_status["enabled"]
    hosts_online = max(0, raw_status["hosts_online"])
    hosts_total = max(0, raw_status["hosts_total"])
    busy_hosts = sum(host["activity"] == "busy" for host in public_hosts)
    summary, tone = _health_summary(enabled, hosts_online, hosts_total, busy_hosts)

    return {
        "enabled": enabled,
        "hosts_online": hosts_online,
        "hosts_total": hosts_total,
        "queued": max(0, raw_status["queue"]["queued"]),
        "busy_hosts": busy_hosts,
        "summary": summary,
        "tone": tone,
        "hosts": public_hosts,
        "compile_specs": _compile_specs(raw_compile_specs),
        "toolchains": toolchains,
        "toolchain_mismatch": any(not toolchain["agrees"] for toolchain in toolchains),
    }


class PublicJudgehostStatusCache:
    def __init__(
        self,
        source_provider: Callable[
            [], tuple[JudgehostStatus, list[CompileSpecStatus]]
        ],
        *,
        ttl_sec: float = 2.0,
    ) -> None:
        self._source_provider = source_provider
        self._ttl_sec = max(0.0, float(ttl_sec))
        self._cached: PublicJudgehostStatus | None = None
        self._cached_at = 0.0

    def snapshot(self) -> PublicJudgehostStatus:
        now = time.monotonic()
        if self._cached is None or now - self._cached_at > self._ttl_sec:
            raw_status, raw_compile_specs = self._source_provider()
            self._cached = project_public_status(
                raw_status,
                raw_compile_specs,
            )
            self._cached_at = now
        return {
            **self._cached,
            "hosts": [host.copy() for host in self._cached["hosts"]],
            "compile_specs": [spec.copy() for spec in self._cached["compile_specs"]],
            "toolchains": [
                {
                    **toolchain,
                    "versions": [item.copy() for item in toolchain["versions"]],
                }
                for toolchain in self._cached["toolchains"]
            ],
        }
