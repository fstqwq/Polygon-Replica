from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app import main_constants as default_main_constants


class RuntimeValues:
    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = MappingProxyType(dict(values))

    def get(self, key: str, default: object | None = None) -> object:
        return self._values.get(str(key or "").strip(), default)

    def __contains__(self, key: object) -> bool:
        return str(key or "").strip() in self._values

    def __getattr__(self, name: str) -> object:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _default_value_map() -> dict[str, object]:
    values: dict[str, object] = {}
    for name in dir(default_main_constants):
        if not name.isupper():
            continue
        values[name] = getattr(default_main_constants, name)
    return values


def build_runtime_values(overrides: Mapping[str, object] | None = None) -> RuntimeValues:
    values = _default_value_map()
    if overrides:
        for key, value in overrides.items():
            safe_key = str(key or "").strip()
            if safe_key in values:
                values[safe_key] = value
    return RuntimeValues(values)
