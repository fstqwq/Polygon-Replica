from __future__ import annotations

import json
import math
import threading
from typing import TypedDict

from app.db import DB, now_iso
from app.runtime_value import build_runtime_values
from app.service.disk.system_config_store import SystemConfigStore


_RUNTIME_DEFAULTS = build_runtime_values()
_ADMIN_CONFIG_DEFAULTS = dict(_RUNTIME_DEFAULTS.ADMIN_CONFIG_DEFAULTS)
_ADMIN_CONFIG_SPECS = dict(_RUNTIME_DEFAULTS.ADMIN_CONFIG_SPECS)


_BOOL_TRUE = {"1", "true", "yes", "on", "y"}
_BOOL_FALSE = {"0", "false", "no", "off", "n"}


AdminConfigSpec = TypedDict(
    "AdminConfigSpec",
    {
        "type": str,
        "category": str,
        "description": str,
        "min": int | float,
        "max": int | float,
        "unit": str,
        "restart_required": bool,
        "impact": str,
        "choices": list[object] | tuple[object, ...] | set[object],
        "ascii": str,
    },
    total=False,
)

SystemConfigPatchPreview = TypedDict(
    "SystemConfigPatchPreview",
    {
        "normalized": dict[str, object],
        "diff": list[dict[str, object]],
        "changed": int,
        "before": dict[str, object],
        "after": dict[str, object],
    },
)


class SystemConfigService:
    _CATEGORY_ORDER = (
        "Judging",
        "Queue",
        "Judgehost",
        "Toolchain",
        "Limits",
        "UI",
        "Security",
        "Auth",
        "Misc",
    )
    _CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
        ("WORKER_QUEUE_", "Queue"),
        ("INVOCATION_", "Judging"),
        ("JUDGEHOST_", "Judgehost"),
        ("TOOLCHAIN_", "Toolchain"),
        ("VERIFICATION_EXEC_", "Judging"),
        ("RUN_", "Judging"),
        ("RUNTIME_CACHE_", "Limits"),
        ("IMPLICIT_VERIFICATION_", "Judging"),
        ("GENERAL_", "Limits"),
        ("TESTS_SPEC_", "Limits"),
        ("SANDBOX_", "Security"),
        ("WORKSPACE_FILE_", "UI"),
        ("UI_", "UI"),
        ("PREVIEW_", "UI"),
        ("STATEMENT_", "UI"),
        ("SUMMARY_JSON_", "UI"),
        ("SOLUTION_", "UI"),
        ("WORKSPACE_HISTORY_", "UI"),
        ("AUTH_", "Auth"),
        ("FLASH_", "Auth"),
        ("PASSWORD_", "Security"),
        ("LOGIN_RATE_", "Security"),
        ("CONTEST_", "Limits"),
        ("PROBLEM_", "Limits"),
    )

    def __init__(self, db: DB):
        self.db = db
        self._store = SystemConfigStore(db)
        self._lock = threading.Lock()
        self._admin_defaults: dict[str, object] = dict(_ADMIN_CONFIG_DEFAULTS)
        self._admin_specs: dict[str, AdminConfigSpec] = dict(_ADMIN_CONFIG_SPECS)
        self._effective_values: dict[str, object] = dict(self._admin_defaults)

    def refresh(self) -> dict[str, object]:
        with self._lock:
            overrides = self._load_overrides_locked()
            effective = dict(self._admin_defaults)
            effective.update(overrides)
            self._effective_values = effective
            return dict(effective)

    def get(self, key: str, default: object | None = None) -> object:
        key = key.strip()
        with self._lock:
            if key in self._effective_values:
                return self._effective_values[key]
        if key in self._admin_defaults:
            return self._admin_defaults[key]
        return default

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._effective_values)

    def ui_sections(self) -> list[dict[str, object]]:
        with self._lock:
            effective = dict(self._effective_values)
        buckets: dict[str, list[dict[str, object]]] = {}
        for key, spec in self._admin_specs.items():
            row = self._config_row(key, spec, effective)
            category = row["category"]
            if category in buckets:
                buckets[category].append(row)
            else:
                buckets[category] = [row]
        sections: list[dict[str, object]] = []
        for category, rows in sorted(
            buckets.items(),
            key=lambda item: (
                self._category_index(item[0]),
                item[0].lower(),
            ),
        ):
            rows.sort(key=lambda row: row["key"])
            changed_count = sum(1 for row in rows if row["changed"])
            sections.append(
                {
                    "category": category,
                    "slug": self.category_slug(category),
                    "rows": rows,
                    "count": len(rows),
                    "changed_count": changed_count,
                }
            )
        return sections

    def section_by_slug(self, slug: str) -> dict[str, object] | None:
        slug = self.category_slug(slug)
        for section in self.ui_sections():
            if section["slug"] == slug:
                return section
        return None

    @staticmethod
    def category_slug(category: str) -> str:
        token = category.strip().lower()
        if not token:
            return "misc"
        parts: list[str] = []
        current = []
        for ch in token:
            if ch.isalnum():
                current.append(ch)
                continue
            if current:
                parts.append("".join(current))
                current = []
        if current:
            parts.append("".join(current))
        if not parts:
            return "misc"
        return "-".join(parts)

    def validate_patch(self, payload: dict[str, object]) -> SystemConfigPatchPreview:
        with self._lock:
            before = dict(self._effective_values)
        normalized: dict[str, object] = {}
        for payload_key, raw_value in payload.items():
            key = payload_key.strip()
            if key not in self._admin_specs:
                raise ValueError(f"unknown system config key: {key}")
            normalized[key] = self._normalize_value(key, raw_value)
        after = dict(before)
        after.update(normalized)
        diff_rows = self._diff_rows(before, after)
        return {
            "normalized": normalized,
            "diff": diff_rows,
            "changed": len(diff_rows),
            "before": before,
            "after": after,
        }

    def apply_patch(self, payload: dict[str, object], actor_user_id: int) -> dict[str, object]:
        preview = self.validate_patch(payload)
        after = dict(preview["after"])
        self._persist_overrides(after, actor_user_id)
        effective = self.refresh()
        diff_rows = self._diff_rows(
            dict(preview["before"]),
            dict(effective),
        )
        return {
            "changed": len(diff_rows),
            "diff": diff_rows,
            "effective": effective,
        }

    def reset(self) -> dict[str, object]:
        self._store.clear_overrides()
        return self.refresh()

    def _category_for_key(self, key: str, spec: AdminConfigSpec) -> str:
        explicit = spec.get("category")
        if explicit:
            return explicit
        token = key.upper()
        for prefix, category in self._CATEGORY_PREFIXES:
            if token.startswith(prefix):
                return category
        return "Misc"

    def _category_index(self, category: str) -> int:
        token = category
        if token in self._CATEGORY_ORDER:
            return self._CATEGORY_ORDER.index(token)
        return len(self._CATEGORY_ORDER) + 1

    def _config_row(self, key: str, spec: AdminConfigSpec, effective: dict[str, object]) -> dict[str, object]:
        default_value = self._admin_defaults[key]
        current_value = effective.get(key, default_value)
        kind = spec.get("type", "str")
        restart_required = spec.get("restart_required", False)
        impact = spec.get("impact")
        if impact is None:
            impact = "restart" if restart_required else "runtime"
        choices_raw = spec.get("choices")
        choices = [] if choices_raw is None else [str(item) for item in choices_raw]
        description = spec.get("description")
        if description is None:
            description = ""
        unit = spec.get("unit")
        if unit is None:
            unit = ""
        return {
            "key": key,
            "type": kind,
            "category": self._category_for_key(key, spec),
            "description": description,
            "min": spec.get("min"),
            "max": spec.get("max"),
            "unit": unit,
            "restart_required": restart_required,
            "impact": impact,
            "choices": choices,
            "default_value": default_value,
            "current_value": current_value,
            "default_display": self._display_value(kind, default_value),
            "current_display": self._display_value(kind, current_value),
            "changed": current_value != default_value,
            "input_name": f"config_{key}",
        }

    def _diff_rows(self, before: dict[str, object], after: dict[str, object]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for key in self._admin_specs:
            prev = before.get(key, self._admin_defaults[key])
            nxt = after.get(key, self._admin_defaults[key])
            if prev == nxt:
                continue
            spec = self._admin_specs[key]
            kind = spec.get("type", "str")
            restart_required = spec.get("restart_required", False)
            impact = spec.get("impact")
            if impact is None:
                impact = "restart" if restart_required else "runtime"
            rows.append(
                {
                    "key": key,
                    "category": self._category_for_key(key, spec),
                    "type": kind,
                    "restart_required": restart_required,
                    "impact": impact,
                    "before": prev,
                    "after": nxt,
                    "before_display": self._display_value(kind, prev),
                    "after_display": self._display_value(kind, nxt),
                }
            )
        return rows

    def _persist_overrides(self, values: dict[str, object], actor_user_id: int) -> None:
        self._store.replace_overrides(
            keys=list(self._admin_specs.keys()),
            values=values,
            defaults=self._admin_defaults,
            actor_user_id=int(actor_user_id),
            updated_at=now_iso(),
        )

    def _load_overrides_locked(self) -> dict[str, object]:
        rows = self._store.override_rows()
        overrides: dict[str, object] = {}
        for row in rows:
            key = row["key"]
            if key not in self._admin_specs:
                continue
            raw_json = row["value_json"]
            if not raw_json:
                continue
            try:
                parsed = json.loads(raw_json)
            except Exception:
                continue
            try:
                normalized = self._normalize_value(key, parsed)
            except ValueError:
                continue
            if normalized == self._admin_defaults[key]:
                continue
            overrides[key] = normalized
        return overrides

    def _normalize_value(self, key: str, raw_value: object) -> object:
        if key not in self._admin_specs:
            raise ValueError(f"unknown system config key: {key}")
        spec = self._admin_specs[key]
        kind = spec.get("type", "str")
        if kind == "int":
            value = self._normalize_int(raw_value, key)
        elif kind == "float":
            value = self._normalize_float(raw_value, key)
        elif kind == "bool":
            value = self._normalize_bool(raw_value, key)
        elif kind == "str":
            value = self._normalize_str(raw_value, key, spec)
        else:
            raise ValueError(f"unsupported config type for {key}: {kind}")

        if ("min" in spec) and kind in {"int", "float"}:
            minimum = float(spec["min"])
            if float(value) < minimum:
                raise ValueError(f"{key} must be >= {self._display_bound(spec['min'])}")
        if ("max" in spec) and kind in {"int", "float"}:
            maximum = float(spec["max"])
            if float(value) > maximum:
                raise ValueError(f"{key} must be <= {self._display_bound(spec['max'])}")
        choices = spec.get("choices")
        if choices:
            if value not in set(choices):
                values = ", ".join((str(item) for item in choices))
                raise ValueError(f"{key} must be one of: {values}")

        return value

    def _normalize_int(self, raw_value: object, key: str) -> int:
        try:
            if raw_value is True:
                return 1
            if raw_value is False:
                return 0
            text = "" if raw_value is None else str(raw_value).strip()
            if not text:
                raise ValueError
            return int(text)
        except Exception as exc:
            raise ValueError(f"{key} must be an integer") from exc

    def _normalize_float(self, raw_value: object, key: str) -> float:
        try:
            value = float(str(raw_value).strip())
        except Exception as exc:
            raise ValueError(f"{key} must be a number") from exc
        if not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number")
        return value

    def _normalize_bool(self, raw_value: object, key: str) -> bool:
        if raw_value is True:
            return True
        if raw_value is False:
            return False
        text = "" if raw_value is None else str(raw_value).strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError(f"{key} must be a boolean (true/false)")

    def _normalize_str(self, raw_value: object, key: str, spec: AdminConfigSpec) -> str:
        value = "" if raw_value is None else str(raw_value)
        ascii_mode = spec.get("ascii")
        if ascii_mode is None:
            ascii_mode = "printable"
        if ascii_mode in {"none", "off", "false", "0"}:
            return value
        if ascii_mode == "visible":
            min_code = 0x21
            max_code = 0x7E
            hint = "visible ASCII characters (0x21-0x7E)"
        else:
            min_code = 0x20
            max_code = 0x7E
            hint = "printable ASCII characters (0x20-0x7E)"
        for ch in value:
            code = ord(ch)
            if code < min_code or code > max_code:
                raise ValueError(f"{key} must contain only {hint}")
        return value

    def _display_value(self, kind: str, value: object) -> str:
        if kind == "bool":
            return "true" if value else "false"
        if kind == "float":
            text = f"{float(value):.6f}".rstrip("0").rstrip(".")
            return text if text else "0"
        if kind in {"int", "str"}:
            return str(value)
        return json.dumps(value, ensure_ascii=False)

    def _display_bound(self, value: object) -> str:
        try:
            numeric = float(str(value))
        except Exception:
            return str(value)
        if numeric.is_integer():
            return str(int(numeric))
        return str(value)
