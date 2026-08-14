import base64
import binascii
import json
import logging
import shlex
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, cast

from app.config import ConfigValues
from app.db import now_iso
from app.service.judgehost.batch.runtime import JudgehostBatchRuntime
from app.service.platform.hashing import compile_command_digest

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True, slots=True)
class _VersionContext:
    language_id: str
    lease_owner: str


class HostTelemetryEventSink(Protocol):
    def __call__(
        self,
        *,
        hostname: str,
        action: str,
        task_id: str,
        run_id: str,
    ) -> None:
        """Record one accepted host telemetry event."""

        ...


class JudgehostTelemetryState(Protocol):
    """State surface required by optional toolchain telemetry."""

    @property
    def batch_runtime(self) -> JudgehostBatchRuntime:
        ...

    @property
    def config_values(self) -> ConfigValues:
        ...

    @property
    def state_lock(self) -> AbstractContextManager[object]:
        ...

    @property
    def host_toolchains(self) -> dict[str, dict[str, HostToolchainTelemetry]]:
        ...


@dataclass(frozen=True, slots=True)
class ToolchainVersionReport:
    judgetask_id: int
    hostname: str
    compiler: str
    runner: str
    task_id: str
    run_id: str


class ToolchainVersionCollector:
    """Own the optional DOMjudge toolchain-version handshake.

    Version collection is runtime telemetry. It accepts reports only for the
    current case lease and never participates in task execution decisions.
    """

    MAX_VERSION_OUTPUT_BYTES = 8 * 1024
    _MAX_ENCODED_OUTPUT_BYTES = 4 * ((MAX_VERSION_OUTPUT_BYTES + 2) // 3)
    _SKIP_COMPILE_DIGEST = compile_command_digest("skip.compile", [])
    _LANGUAGE_IDS = frozenset({"c", "cpp", "java", "py"})

    def __init__(self, state: JudgehostTelemetryState) -> None:
        self._state = state

    @staticmethod
    def _config_object(raw: str) -> dict[str, object]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("judgehost toolchain config must be an object")
        return cast(dict[str, object], payload)

    def _version_context(self, judgetask_id: int) -> _VersionContext | None:
        case = self._state.batch_runtime.fetch_case(judgetask_id)
        if case is None or case["status"] != "leased":
            return None
        batch = self._state.batch_runtime.fetch_batch(case["batch_id"])
        if batch is None:
            return None
        compile_config = self._config_object(batch["compile_config_json"])
        if compile_config["toolchain_cmd_digest"] == self._SKIP_COMPILE_DIGEST:
            return None
        run_config = self._config_object(batch["run_config_json"])
        language_id = cast(str, run_config["language_id"])
        if language_id not in self._LANGUAGE_IDS:
            raise RuntimeError(f"unsupported judgehost toolchain language: {language_id}")
        return _VersionContext(
            language_id=language_id,
            lease_owner=case["lease_owner"],
        )

    @staticmethod
    def _binary_version_script(command: str, *arguments: str) -> str:
        executable = shlex.quote(command)
        argv = " ".join(shlex.quote(argument) for argument in arguments)
        invocation = executable if not argv else f"{executable} {argv}"
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"resolved=$(command -v {executable})\n"
            "printf 'command=%s\\n' \"$resolved\"\n"
            f"exec {invocation} 2>&1\n"
        )

    @staticmethod
    def _python_version_script() -> str:
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            "for candidate in pypy3 python3 python; do\n"
            '    if resolved=$(command -v "$candidate" 2>/dev/null); then\n'
            "        printf 'command=%s\\n' \"$resolved\"\n"
            '        exec "$candidate" --version 2>&1\n'
            "    fi\n"
            "done\n"
            "echo 'python interpreter not found (tried: pypy3, python3, python)' >&2\n"
            "exit 127\n"
        )

    def version_commands(self, judgetask_id: int) -> dict[str, object]:
        context = self._version_context(int(judgetask_id))
        if context is None:
            return {}
        config_values = self._state.config_values
        if context.language_id in {"c", "cpp"}:
            compiler = config_values.TOOLCHAIN_CPP_COMPILER
            return {
                "compiler_version_command": self._binary_version_script(compiler, "--version"),
            }
        if context.language_id == "java":
            compiler = config_values.TOOLCHAIN_JAVA_COMPILER
            return {
                "compiler_version_command": self._binary_version_script(compiler, "-version"),
                "runner_version_command": self._binary_version_script("java", "-version"),
            }
        python_script = self._python_version_script()
        return {
            "compiler_version_command": python_script,
            "runner_version_command": python_script,
        }

    @classmethod
    def _decode_version_output(cls, encoded: str, *, field: str) -> str | None:
        token = encoded.strip()
        if not token:
            return None
        try:
            encoded_bytes = token.encode("ascii")
        except UnicodeEncodeError:
            logger.warning("judgehost toolchain %s version is not base64 ASCII", field)
            return None
        if len(encoded_bytes) > cls._MAX_ENCODED_OUTPUT_BYTES:
            logger.warning("judgehost toolchain %s version exceeds encoded size limit", field)
            return None
        try:
            raw = base64.b64decode(encoded_bytes, validate=True)
        except (binascii.Error, ValueError):
            logger.warning("judgehost toolchain %s version is not valid base64", field)
            return None
        if len(raw) > cls.MAX_VERSION_OUTPUT_BYTES:
            logger.warning("judgehost toolchain %s version exceeds decoded size limit", field)
            return None
        text = raw.decode("utf-8", errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "\ufffd").strip()
        return text or None

    def record_report(
        self,
        judgetask_id: int,
        *,
        hostname: str,
        compiler: str,
        runner: str,
    ) -> bool:
        context = self._version_context(int(judgetask_id))
        if context is None:
            logger.warning(
                "ignored judgehost toolchain report for inactive judgetask_id=%s",
                judgetask_id,
            )
            return False
        if context.lease_owner != hostname:
            logger.warning(
                "ignored judgehost toolchain report for non-owner judgetask_id=%s hostname=%s",
                judgetask_id,
                hostname,
            )
            return False
        compiler_version = self._decode_version_output(compiler, field="compiler")
        runner_version = self._decode_version_output(runner, field="runner")
        if compiler_version is None and runner_version is None:
            return False
        telemetry = HostToolchainTelemetry(
            language_id=context.language_id,
            compiler=compiler_version or "",
            runner=runner_version or "",
            observed_at=now_iso(),
            judgetask_id=int(judgetask_id),
        )
        with self._state.state_lock:
            host_toolchains = self._state.host_toolchains.setdefault(hostname, {})
            host_toolchains[context.language_id] = telemetry
        return True


class ToolchainTelemetryHandler:
    """Contain optional telemetry failures outside callback orchestration."""

    def __init__(
        self,
        state: JudgehostTelemetryState,
        event_sink: HostTelemetryEventSink,
    ) -> None:
        self._collector = ToolchainVersionCollector(state)
        self._event_sink = event_sink

    def version_commands(self, judgetask_id: int) -> dict[str, object]:
        """Return optional commands while containing telemetry-only failures."""

        try:
            return self._collector.version_commands(int(judgetask_id))
        except Exception:
            logger.exception(
                "failed to prepare judgehost toolchain version commands " "judgetask_id=%s",
                judgetask_id,
            )
            return {}

    def record_report(
        self,
        report: ToolchainVersionReport,
    ) -> None:
        """Store an authenticated report without affecting task completion."""

        try:
            recorded = self._collector.record_report(
                int(report.judgetask_id),
                hostname=report.hostname,
                compiler=report.compiler,
                runner=report.runner,
            )
        except Exception:
            logger.exception(
                "failed to record judgehost toolchain versions " "judgetask_id=%s hostname=%s",
                report.judgetask_id,
                report.hostname,
            )
            return
        if recorded:
            try:
                self._event_sink(
                    hostname=report.hostname,
                    action="versions",
                    task_id=report.task_id,
                    run_id=report.run_id,
                )
            except Exception:
                logger.exception(
                    "failed to record judgehost toolchain host event "
                    "judgetask_id=%s hostname=%s",
                    report.judgetask_id,
                    report.hostname,
                )
