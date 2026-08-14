"""Authoritative registry and cross-field validation for system config."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.config.definitions import CONFIG_DEFINITIONS
from app.config.model import ConfigDefinition, ConfigKind, ConfigValues


def _canonical_int(values: Mapping[str, object], key: str) -> int:
    value = values[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"normalized system config {key} is not an integer")
    return value


@dataclass(frozen=True)
class ConfigRegistry:
    """Validated, ordered configuration definitions."""

    definitions: tuple[ConfigDefinition, ...]

    def __post_init__(self) -> None:
        keys = [definition.key for definition in self.definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate system configuration key")
        if any(not definition.category for definition in self.definitions):
            raise ValueError("system configuration category is required")
        defaults = self.defaults()
        self.validate_snapshot(defaults)

    @classmethod
    def from_definitions(cls, definitions: Iterable[ConfigDefinition]) -> "ConfigRegistry":
        """Build and validate a registry while preserving definition order."""

        return cls(tuple(definitions))

    @property
    def by_key(self) -> Mapping[str, ConfigDefinition]:
        """Return definitions indexed by their durable configuration key."""

        return {definition.key: definition for definition in self.definitions}

    def defaults(self) -> dict[str, object]:
        """Return the normalized default snapshot."""

        return {
            definition.key: definition.normalize(definition.default)
            for definition in self.definitions
        }

    def normalize(self, key: str, raw_value: object) -> object:
        """Normalize one external value using its registered definition."""

        definition = self.by_key.get(key)
        if definition is None:
            raise ValueError(f"unknown system config key: {key}")
        return definition.normalize(raw_value)

    def normalize_snapshot(
        self,
        values: Mapping[str, object],
    ) -> dict[str, object]:
        """Normalize and cross-validate one complete configuration snapshot."""

        expected = set(self.by_key)
        missing = expected - set(values)
        if missing:
            raise ValueError(f"system config values missing: {', '.join(sorted(missing))}")
        extra = set(values) - expected
        if extra:
            raise ValueError(f"unknown system config values: {', '.join(sorted(extra))}")
        normalized = {
            definition.key: definition.normalize(values[definition.key])
            for definition in self.definitions
        }
        cookie_names = (
            normalized["AUTH_COOKIE_NAME"],
            normalized["SUDO_COOKIE_NAME"],
            normalized["FLASH_COOKIE_NAME"],
        )
        if len(set(cookie_names)) != len(cookie_names):
            raise ValueError(
                "AUTH_COOKIE_NAME, SUDO_COOKIE_NAME, and FLASH_COOKIE_NAME must be distinct"
            )
        for minimum_key, maximum_key in (
            ("GENERAL_TIME_LIMIT_MIN_MS", "GENERAL_TIME_LIMIT_MAX_MS"),
            ("GENERAL_MEMORY_LIMIT_MIN_MB", "GENERAL_MEMORY_LIMIT_MAX_MB"),
            ("GENERAL_PASS_LIMIT_MIN", "GENERAL_PASS_LIMIT_MAX"),
        ):
            if _canonical_int(normalized, minimum_key) > _canonical_int(normalized, maximum_key):
                raise ValueError(f"{minimum_key} must be <= {maximum_key}")
        if _canonical_int(normalized, "STATEMENT_SAMPLE_MAX_BYTES") > _canonical_int(
            normalized, "TEXTAREA_MAX_BYTES"
        ):
            raise ValueError("STATEMENT_SAMPLE_MAX_BYTES must be <= TEXTAREA_MAX_BYTES")
        return normalized

    def validate_snapshot(self, values: Mapping[str, object]) -> None:
        """Raise when a complete configuration snapshot is invalid."""

        self.normalize_snapshot(values)

    @staticmethod
    def display_value(kind: ConfigKind, value: object) -> str:
        """Render a canonical scalar for the admin configuration form."""

        if kind is ConfigKind.BOOL:
            return "true" if value else "false"
        if kind is ConfigKind.FLOAT:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RuntimeError("normalized float config is not numeric")
            text = f"{value:.6f}".rstrip("0").rstrip(".")
            return text if text else "0"
        if kind in {ConfigKind.INT, ConfigKind.STR}:
            return str(value)
        raise AssertionError(f"unsupported config kind: {kind}")


CONFIG_REGISTRY = ConfigRegistry.from_definitions(CONFIG_DEFINITIONS)


def build_config_values(
    overrides: Mapping[str, object] | None = None,
    *,
    registry: ConfigRegistry = CONFIG_REGISTRY,
) -> ConfigValues:
    """Build one validated effective configuration snapshot."""

    values = registry.defaults()
    if overrides:
        for key, raw_value in overrides.items():
            values[key] = registry.normalize(key, raw_value)
    return ConfigValues(values, normalizer=registry.normalize_snapshot)
