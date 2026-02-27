from __future__ import annotations

import json
import math
import threading

from app.db import DB, now_iso
from app.runtime_values import build_runtime_values


_RUNTIME_DEFAULTS = build_runtime_values()
_ADMIN_CONFIG_DEFAULTS = dict(_RUNTIME_DEFAULTS.ADMIN_CONFIG_DEFAULTS)
_ADMIN_CONFIG_SPECS = dict(_RUNTIME_DEFAULTS.ADMIN_CONFIG_SPECS)


_BOOL_TRUE = {"1", "true", "yes", "on", "y"}
_BOOL_FALSE = {"0", "false", "no", "off", "n"}


class SystemConfigService:
    def __init__(self, db: DB):
        self.db = db
        self._lock = threading.Lock()
        self._admin_defaults: dict[str, object] = dict(_ADMIN_CONFIG_DEFAULTS)
        self._admin_specs: dict[str, dict[str, object]] = dict(_ADMIN_CONFIG_SPECS)
        self._effective_values: dict[str, object] = dict(self._admin_defaults)

    def refresh(self) -> dict[str, object]:
        with self._lock:
            overrides = self._load_overrides_locked()
            effective = dict(self._admin_defaults)
            effective.update(overrides)
            self._effective_values = effective
            return dict(effective)

    def get(self, key: str, default: object | None = None) -> object:
        key_text = str(key or "").strip()
        with self._lock:
            if key_text in self._effective_values:
                return self._effective_values[key_text]
        if key_text in self._admin_defaults:
            return self._admin_defaults[key_text]
        return default

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._effective_values)

    def ui_rows(self) -> list[dict[str, object]]:
        with self._lock:
            effective = dict(self._effective_values)
        rows: list[dict[str, object]] = []
        for key, spec in self._admin_specs.items():
            default_value = self._admin_defaults[key]
            current_value = effective.get(key, default_value)
            rows.append(
                {
                    "key": key,
                    "type": str(spec.get("type") or "str"),
                    "description": str(spec.get("description") or ""),
                    "min": spec.get("min"),
                    "max": spec.get("max"),
                    "default_value": default_value,
                    "current_value": current_value,
                    "default_display": self._display_value(default_value),
                    "current_display": self._display_value(current_value),
                    "changed": current_value != default_value,
                }
            )
        return rows

    def update_from_payload(self, payload: dict[str, object], actor_user_id: int) -> dict[str, object]:
        normalized: dict[str, object] = {}
        if not isinstance(payload, dict):
            raise ValueError("system config payload must be a JSON object")
        for payload_key in payload:
            key_text = str(payload_key or "").strip()
            if key_text not in self._admin_specs:
                raise ValueError(f"unknown system config key: {key_text}")
        for key in self._admin_specs:
            if key in payload:
                normalized[key] = self._normalize_value(key, payload[key])
            else:
                normalized[key] = self._effective_values.get(key, self._admin_defaults[key])
        self._persist_overrides(normalized, actor_user_id)
        return self.refresh()

    def reset(self) -> dict[str, object]:
        with self.db.conn() as conn:
            conn.execute("DELETE FROM system_config")
            conn.commit()
        return self.refresh()

    def _persist_overrides(self, values: dict[str, object], actor_user_id: int) -> None:
        safe_actor_user_id = int(actor_user_id)
        when = now_iso()
        with self.db.conn() as conn:
            for key in self._admin_specs:
                value = values[key]
                default = self._admin_defaults[key]
                if value == default:
                    conn.execute("DELETE FROM system_config WHERE key=?", [key])
                    continue
                conn.execute(
                    """
                    INSERT INTO system_config(key, value_json, updated_at, updated_by_user_id)
                    VALUES(?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at,
                        updated_by_user_id=excluded.updated_by_user_id
                    """,
                    [key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), when, safe_actor_user_id],
                )
            conn.execute(
                "DELETE FROM system_config WHERE key NOT IN ({})".format(
                    ",".join("?" for _ in self._admin_specs)
                ),
                list(self._admin_specs.keys()),
            )
            conn.commit()

    def _load_overrides_locked(self) -> dict[str, object]:
        rows = self.db.fetch_all("SELECT key, value_json FROM system_config ORDER BY key ASC")
        overrides: dict[str, object] = {}
        for row in rows:
            key = str(row["key"] or "").strip()
            if key not in self._admin_specs:
                continue
            raw_json = str(row["value_json"] or "").strip()
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
        kind = str(spec.get("type") or "str").strip().lower()
        if kind == "int":
            value = self._normalize_int(raw_value, key)
        elif kind == "float":
            value = self._normalize_float(raw_value, key)
        elif kind == "bool":
            value = self._normalize_bool(raw_value, key)
        elif kind == "str":
            value = self._normalize_str(raw_value)
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
        if isinstance(choices, (list, tuple, set)) and choices:
            if value not in set(choices):
                values = ", ".join((str(item) for item in choices))
                raise ValueError(f"{key} must be one of: {values}")

        return value

    def _normalize_int(self, raw_value: object, key: str) -> int:
        try:
            if isinstance(raw_value, bool):
                return int(raw_value)
            if isinstance(raw_value, int):
                return raw_value
            if isinstance(raw_value, float):
                if not math.isfinite(raw_value):
                    raise ValueError
                if abs(raw_value - int(raw_value)) > 1e-9:
                    raise ValueError
                return int(raw_value)
            text = str(raw_value or "").strip()
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
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, int):
            if raw_value in {0, 1}:
                return bool(raw_value)
        text = str(raw_value or "").strip().lower()
        if text in _BOOL_TRUE:
            return True
        if text in _BOOL_FALSE:
            return False
        raise ValueError(f"{key} must be a boolean (true/false)")

    def _normalize_str(self, raw_value: object) -> str:
        return str(raw_value if raw_value is not None else "")

    def _display_value(self, value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, float):
            text = f"{value:.6f}".rstrip("0").rstrip(".")
            return text if text else "0"
        if isinstance(value, (int, str)):
            return str(value)
        return json.dumps(value, ensure_ascii=False)

    def _display_bound(self, value: object) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
