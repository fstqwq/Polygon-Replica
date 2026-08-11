"""Typed definitions and atomically replaceable effective configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import math
import re
import threading
from types import MappingProxyType
from typing import Callable


class ConfigKind(str, Enum):
    """Scalar shapes supported by durable system configuration."""

    BOOL = "bool"
    FLOAT = "float"
    INT = "int"
    STR = "str"


class TextPolicy(str, Enum):
    """Character and syntax policy for a string configuration value."""

    ANY = "any"
    PRINTABLE_ASCII = "printable-ascii"
    VISIBLE_ASCII = "visible-ascii"
    COOKIE_NAME = "cookie-name"
    REGEX = "regex"


_BOOL_TRUE = frozenset({"1", "true", "yes", "on", "y"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off", "n"})
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


@dataclass(frozen=True)
class ConfigDefinition:
    """One authoritative admin-editable configuration definition."""

    key: str
    kind: ConfigKind
    default: object
    category: str
    description: str
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[object, ...] = ()
    text_policy: TextPolicy | None = None
    restart_required: bool = False

    @property
    def impact(self) -> str:
        return "restart" if self.restart_required else "runtime"

    def normalize(self, raw_value: object) -> object:
        """Normalize and validate one external value."""

        if self.kind is ConfigKind.INT:
            value = self._normalize_int(raw_value)
        elif self.kind is ConfigKind.FLOAT:
            value = self._normalize_float(raw_value)
        elif self.kind is ConfigKind.BOOL:
            value = self._normalize_bool(raw_value)
        else:
            value = self._normalize_str(raw_value)
        self._validate_bounds(value)
        if self.choices and value not in self.choices:
            choices = ", ".join(str(item) for item in self.choices)
            raise ValueError(f"{self.key} must be one of: {choices}")
        return value

    def _normalize_int(self, raw_value: object) -> int:
        try:
            if isinstance(raw_value, bool):
                raise ValueError
            text = "" if raw_value is None else str(raw_value).strip()
            if not text:
                raise ValueError
            return int(text)
        except Exception as exc:
            raise ValueError(f"{self.key} must be an integer") from exc

    def _normalize_float(self, raw_value: object) -> float:
        try:
            if isinstance(raw_value, bool):
                raise ValueError
            value = float(str(raw_value).strip())
        except Exception as exc:
            raise ValueError(f"{self.key} must be a number") from exc
        if not math.isfinite(value):
            raise ValueError(f"{self.key} must be a finite number")
        return value

    def _normalize_bool(self, raw_value: object) -> bool:
        if raw_value is True:
            return True
        if raw_value is False:
            return False
        text = "" if raw_value is None else str(raw_value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError(f"{self.key} must be a boolean (true/false)")

    def _normalize_str(self, raw_value: object) -> str:
        value = "" if raw_value is None else str(raw_value)
        policy = self.text_policy or TextPolicy.PRINTABLE_ASCII
        if policy is TextPolicy.COOKIE_NAME:
            if _COOKIE_NAME_RE.fullmatch(value) is None:
                raise ValueError(f"{self.key} must be a valid HTTP cookie token")
        elif policy is TextPolicy.VISIBLE_ASCII:
            self._validate_ascii(value, 0x21, "visible ASCII characters (0x21-0x7E)")
        elif policy is TextPolicy.PRINTABLE_ASCII:
            self._validate_ascii(value, 0x20, "printable ASCII characters (0x20-0x7E)")
        elif policy is TextPolicy.REGEX:
            self._validate_ascii(value, 0x21, "visible ASCII characters (0x21-0x7E)")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{self.key} must be a valid regular expression") from exc
        return value

    def _validate_ascii(self, value: str, minimum: int, hint: str) -> None:
        if any(ord(ch) < minimum or ord(ch) > 0x7E for ch in value):
            raise ValueError(f"{self.key} must contain only {hint}")

    def _validate_bounds(self, value: object) -> None:
        if self.minimum is not None:
            actual = len(value) if self.kind is ConfigKind.STR else float(value)
            if actual < self.minimum:
                label = " length" if self.kind is ConfigKind.STR else ""
                raise ValueError(
                    f"{self.key}{label} must be >= {self._bound(self.minimum)}"
                )
        if self.maximum is not None:
            actual = len(value) if self.kind is ConfigKind.STR else float(value)
            if actual > self.maximum:
                label = " length" if self.kind is ConfigKind.STR else ""
                raise ValueError(
                    f"{self.key}{label} must be <= {self._bound(self.maximum)}"
                )

    @staticmethod
    def _bound(value: int | float) -> str:
        numeric = float(value)
        return str(int(numeric)) if numeric.is_integer() else str(value)


class ConfigValues:
    """Thread-safe holder for one immutable effective configuration snapshot."""

    def __init__(
        self,
        values: Mapping[str, object],
        *,
        normalizer: Callable[[Mapping[str, object]], Mapping[str, object]],
    ) -> None:
        self._lock = threading.RLock()
        self._normalizer = normalizer
        candidate = dict(self._normalizer(values))
        self._values: Mapping[str, object] = MappingProxyType(candidate)

    def replace(self, values: Mapping[str, object]) -> None:
        candidate = dict(self._normalizer(values))
        with self._lock:
            self._values = MappingProxyType(candidate)

    def snapshot(self) -> Mapping[str, object]:
        with self._lock:
            return self._values

    def get(self, key: str, default: object | None = None) -> object:
        with self._lock:
            return self._values.get(key, default)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._values

    def __getattr__(self, name: str) -> object:
        with self._lock:
            try:
                return self._values[name]
            except KeyError as exc:
                raise AttributeError(name) from exc
