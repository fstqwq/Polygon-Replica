"""Thread-safe runtime configuration values."""

from __future__ import annotations

from collections.abc import Mapping
import threading

from app import main_constant


class RuntimeValues:
    """Mutable runtime constants snapshot shared by services."""

    def __init__(self, values: Mapping[str, object]) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, object] = dict(values)

    def replace(self, values: Mapping[str, object]) -> None:
        """Replace all runtime values atomically."""

        with self._lock:
            self._values = dict(values)

    def to_dict(self) -> dict[str, object]:
        """Return a shallow copy of all runtime values."""

        with self._lock:
            return dict(self._values)

    def get(self, key: str, default: object | None = None) -> object:
        """Return a runtime value by key."""

        token = str(key or "").strip()
        with self._lock:
            return self._values.get(token, default)

    def __contains__(self, key: object) -> bool:
        token = str(key or "").strip()
        with self._lock:
            return token in self._values

    def __getattr__(self, name: str) -> object:
        with self._lock:
            if name in self._values:
                return self._values[name]
        raise AttributeError(name)


def _default_value_map() -> dict[str, object]:
    """Collect default runtime constants from app.main_constant."""

    values: dict[str, object] = {}
    for name in dir(main_constant):
        if not name.isupper():
            continue
        values[name] = getattr(main_constant, name)
    return values


def build_runtime_values(overrides: Mapping[str, object] | None = None) -> RuntimeValues:
    """Build runtime values with optional scalar overrides."""

    values = _default_value_map()
    if overrides:
        for key, value in overrides.items():
            safe_key = str(key or "").strip()
            if safe_key in values:
                values[safe_key] = value
    return RuntimeValues(values)
