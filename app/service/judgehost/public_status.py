from __future__ import annotations

import re
import time
from pathlib import PurePosixPath
from typing import Callable, TypedDict


_LANGUAGE_LABELS = {"c": "C", "cpp": "C++", "java": "Java", "py": "Python"}
_PRIVATE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)[^\s]+")


class PublicCompileSpec(TypedDict):
    language_id: str
    language_label: str
    command: str
    arguments: list[str]


class PublicToolchainEntry(TypedDict):
    language_id: str
    language_label: str
    compiler: str
    runner: str


class PublicToolchainProfile(TypedDict):
    label: str
    toolchains: list[PublicToolchainEntry]
    host_count: int


class PublicJudgehostView(TypedDict):
    label: str
    state: str
    tone: str
    last_contact: str
    active_tasks: int
    judged_cases: int
    recent_average: str
    toolchain_profile: str


class PublicJudgehostStatus(TypedDict):
    enabled: bool
    hosts_online: int
    hosts_total: int
    queued: int
    active: int
    summary: str
    tone: str
    hosts: list[PublicJudgehostView]
    compile_specs: list[PublicCompileSpec]
    toolchain_profiles: list[PublicToolchainProfile]
    toolchain_warning: str


def _duration_label(age_sec: object) -> str:
    if not isinstance(age_sec, int) or age_sec < 0:
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
    token = str(raw).replace("\\", "/").strip()
    return PurePosixPath(token).name or "unknown"


def _safe_argument(raw: object) -> str:
    token = " ".join(str(raw).split())[:240]
    return _PRIVATE_PATH_RE.sub("[path]", token)


def _safe_version_lines(raw: object) -> tuple[str, str]:
    lines: list[str] = []
    for raw_line in str(raw or "").replace("\r", "\n").splitlines():
        line = " ".join(raw_line.split())
        if not line or line.startswith("command="):
            continue
        line = _PRIVATE_PATH_RE.sub("[path]", line)
        lines.append(line[:240])
    if not lines:
        return "", ""
    return lines[0], "\n".join(lines)


def _toolchain_key(raw_toolchains: object) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    if isinstance(raw_toolchains, list):
        for raw in raw_toolchains:
            if not isinstance(raw, dict):
                continue
            language_id = str(raw.get("language_id") or "")
            if language_id not in _LANGUAGE_LABELS:
                continue
            _compiler_display, compiler_key = _safe_version_lines(raw.get("compiler"))
            _runner_display, runner_key = _safe_version_lines(raw.get("runner"))
            entries.append((language_id, compiler_key, runner_key))
    return tuple(sorted(entries))


def _toolchain_entries(key: tuple[tuple[str, str, str], ...]) -> list[PublicToolchainEntry]:
    entries: list[PublicToolchainEntry] = []
    for language_id, compiler_raw, runner_raw in key:
        compiler = compiler_raw.splitlines()[0] if compiler_raw else "not reported"
        runner = runner_raw.splitlines()[0] if runner_raw else ""
        entries.append(
            {
                "language_id": language_id,
                "language_label": _LANGUAGE_LABELS[language_id],
                "compiler": compiler,
                "runner": runner,
            }
        )
    return entries


def _profile_label(index: int) -> str:
    number = max(0, index)
    label = ""
    while True:
        number, remainder = divmod(number, 26)
        label = chr(ord("A") + remainder) + label
        if number == 0:
            return label
        number -= 1


def _toolchain_profiles(
    online_hosts: list[dict[str, object]],
) -> tuple[
    dict[tuple[tuple[str, str, str], ...], str],
    list[PublicToolchainProfile],
    str,
]:
    profile_counts: dict[tuple[tuple[str, str, str], ...], int] = {}
    for raw in online_hosts:
        key = _toolchain_key(raw.get("toolchains"))
        profile_counts[key] = profile_counts.get(key, 0) + 1
    profile_keys = sorted(profile_counts, key=repr)
    labels_by_key = {key: _profile_label(index) for index, key in enumerate(profile_keys)}

    expected_languages = set(_LANGUAGE_LABELS)
    incomplete = any({item[0] for item in key} != expected_languages for key in profile_keys)
    versions_by_language: dict[str, set[tuple[str, str]]] = {}
    for key in profile_keys:
        for language_id, compiler, runner in key:
            versions_by_language.setdefault(language_id, set()).add((compiler, runner))
    inconsistent = any(len(versions) > 1 for versions in versions_by_language.values())
    warning = ""
    if online_hosts and inconsistent:
        warning = "Online judgehosts report different toolchains."
    elif online_hosts and incomplete:
        warning = "Toolchain reports are incomplete."
    profiles = [
        {
            "label": labels_by_key[key],
            "toolchains": _toolchain_entries(key),
            "host_count": profile_counts[key],
        }
        for key in profile_keys
    ]
    return labels_by_key, profiles, warning


def _public_hosts(
    hosts_source: list[dict[str, object]],
    labels_by_key: dict[tuple[tuple[str, str, str], ...], str],
) -> list[PublicJudgehostView]:
    hosts: list[PublicJudgehostView] = []
    for index, raw in enumerate(hosts_source, start=1):
        enabled = bool(raw.get("enabled"))
        online = bool(raw.get("online"))
        state = "disabled" if not enabled else "online" if online else "offline"
        recent_raw = raw.get("recent_avg_per_case_sec")
        recent_average = (
            f"{float(recent_raw):.3f}s per case"
            if isinstance(recent_raw, (int, float))
            else "not available"
        )
        profile = labels_by_key.get(_toolchain_key(raw.get("toolchains")), "") if enabled and online else ""
        hosts.append(
            {
                "label": f"Judgehost {index}",
                "state": state,
                "tone": "muted" if state == "disabled" else "ok" if state == "online" else "danger",
                "last_contact": _duration_label(raw.get("age_sec")),
                "active_tasks": max(0, int(raw.get("active_leases") or 0)),
                "judged_cases": max(0, int(raw.get("judged_case_count") or 0)),
                "recent_average": recent_average,
                "toolchain_profile": profile,
            }
        )
    return hosts


def _health_summary(enabled: bool, hosts_online: int, hosts_total: int) -> tuple[str, str]:
    if not enabled:
        return "disabled", "muted"
    if hosts_online <= 0:
        return "offline", "danger"
    if hosts_online < hosts_total:
        return f"{hosts_online}/{hosts_total} online", "warn"
    return f"{hosts_online} online", "ok"


def _compile_specs(raw_compile_specs: list[dict[str, object]]) -> list[PublicCompileSpec]:
    specs: list[PublicCompileSpec] = []
    for raw in raw_compile_specs:
        language_id = str(raw.get("language_id") or "")
        arguments_raw = raw.get("arguments")
        arguments = [_safe_argument(value) for value in arguments_raw] if isinstance(arguments_raw, list) else []
        specs.append(
            {
                "language_id": language_id,
                "language_label": _LANGUAGE_LABELS.get(language_id, language_id or "Unknown"),
                "command": _safe_command(str(raw.get("command") or "")),
                "arguments": arguments,
            }
        )
    return specs


def project_public_status(
    raw_status: dict[str, object],
    raw_compile_specs: list[dict[str, object]],
) -> PublicJudgehostStatus:
    raw_hosts = raw_status.get("hosts")
    hosts_source = [raw for raw in raw_hosts if isinstance(raw, dict)] if isinstance(raw_hosts, list) else []
    online_hosts = [raw for raw in hosts_source if bool(raw.get("enabled")) and bool(raw.get("online"))]
    labels_by_key, profiles, warning = _toolchain_profiles(online_hosts)
    public_hosts = _public_hosts(hosts_source, labels_by_key)

    enabled = bool(raw_status.get("enabled"))
    hosts_online = max(0, int(raw_status.get("hosts_online") or 0))
    hosts_total = max(0, int(raw_status.get("hosts_total") or 0))
    summary, tone = _health_summary(enabled, hosts_online, hosts_total)

    queue_raw = raw_status.get("queue")
    queue = queue_raw if isinstance(queue_raw, dict) else {}
    return {
        "enabled": enabled,
        "hosts_online": hosts_online,
        "hosts_total": hosts_total,
        "queued": max(0, int(queue.get("queued") or 0)),
        "active": sum(host["active_tasks"] for host in public_hosts),
        "summary": summary,
        "tone": tone,
        "hosts": public_hosts,
        "compile_specs": _compile_specs(raw_compile_specs),
        "toolchain_profiles": profiles,
        "toolchain_warning": warning,
    }


class PublicJudgehostStatusCache:
    def __init__(
        self,
        status_provider: Callable[[], dict[str, object]],
        compile_spec_provider: Callable[[], list[dict[str, object]]],
        *,
        ttl_sec: float = 2.0,
    ) -> None:
        self._status_provider = status_provider
        self._compile_spec_provider = compile_spec_provider
        self._ttl_sec = max(0.0, float(ttl_sec))
        self._cached: PublicJudgehostStatus | None = None
        self._cached_at = 0.0

    def snapshot(self) -> PublicJudgehostStatus:
        now = time.monotonic()
        if self._cached is None or now - self._cached_at > self._ttl_sec:
            self._cached = project_public_status(
                self._status_provider(),
                self._compile_spec_provider(),
            )
            self._cached_at = now
        return {
            **self._cached,
            "hosts": [dict(host) for host in self._cached["hosts"]],
            "compile_specs": [dict(spec) for spec in self._cached["compile_specs"]],
            "toolchain_profiles": [
                {**profile, "toolchains": [dict(item) for item in profile["toolchains"]]}
                for profile in self._cached["toolchain_profiles"]
            ],
        }
