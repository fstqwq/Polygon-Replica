from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.config import ConfigValues
from app.service.problem.runtime_config import ProblemConfigLimits


@dataclass(frozen=True, slots=True)
class JudgehostSettings:
    values: Mapping[str, object]
    enabled: bool
    api_token: str
    api_username: str
    fetch_batch_size: int
    wait_timeout_sec: int
    wait_poll_sec: float
    online_window_sec: int
    max_submission_source_bytes: int
    max_tests_per_task: int
    max_component_source_bytes: int
    problem_config_limits: ProblemConfigLimits


class JudgehostConfiguration:
    """Decode one canonical Judgehost settings snapshot per operation."""

    def __init__(self, values: ConfigValues) -> None:
        self._values = values

    @staticmethod
    def _bool(values: Mapping[str, object], key: str) -> bool:
        value = values[key]
        if not isinstance(value, bool):
            raise RuntimeError(f"invalid internal boolean configuration: {key}")
        return value

    @staticmethod
    def _int(values: Mapping[str, object], key: str) -> int:
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"invalid internal integer configuration: {key}")
        return value

    @staticmethod
    def _float(values: Mapping[str, object], key: str) -> float:
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"invalid internal numeric configuration: {key}")
        return float(value)

    @staticmethod
    def _text(values: Mapping[str, object], key: str) -> str:
        value = values[key]
        if not isinstance(value, str):
            raise RuntimeError(f"invalid internal text configuration: {key}")
        return value

    def snapshot(self) -> JudgehostSettings:
        values = MappingProxyType(dict(self._values.snapshot()))
        return JudgehostSettings(
            values=values,
            enabled=self._bool(values, "JUDGEHOST_ENABLE"),
            api_token=self._text(values, "JUDGEHOST_API_TOKEN"),
            api_username=self._text(values, "JUDGEHOST_API_USERNAME"),
            fetch_batch_size=self._int(values, "JUDGEHOST_FETCH_BATCH_SIZE"),
            wait_timeout_sec=self._int(values, "JUDGEHOST_WAIT_TIMEOUT_SEC"),
            wait_poll_sec=self._float(values, "JUDGEHOST_WAIT_POLL_SEC"),
            online_window_sec=self._int(values, "JUDGEHOST_ONLINE_WINDOW_SEC"),
            max_submission_source_bytes=self._int(
                values, "JUDGEHOST_MAX_SUBMISSION_SOURCE_BYTES"
            ),
            max_tests_per_task=self._int(values, "JUDGEHOST_MAX_TESTS_PER_TASK"),
            max_component_source_bytes=self._int(
                values, "JUDGEHOST_MAX_COMPONENT_SOURCE_BYTES"
            ),
            problem_config_limits=ProblemConfigLimits(
                min_time_limit_ms=self._int(values, "GENERAL_TIME_LIMIT_MIN_MS"),
                max_time_limit_ms=self._int(values, "GENERAL_TIME_LIMIT_MAX_MS"),
                min_memory_limit_mb=self._int(values, "GENERAL_MEMORY_LIMIT_MIN_MB"),
                max_memory_limit_mb=self._int(values, "GENERAL_MEMORY_LIMIT_MAX_MB"),
                min_pass_limit=self._int(values, "GENERAL_PASS_LIMIT_MIN"),
                max_pass_limit=self._int(values, "GENERAL_PASS_LIMIT_MAX"),
            ),
        )
