"""Durable overrides and active snapshots for typed system configuration."""

from __future__ import annotations

import json
import threading
from typing import TypedDict

from app.config.model import ConfigDefinition
from app.config.registry import CONFIG_REGISTRY, ConfigRegistry
from app.db import DB, now_iso
from app.service.disk.system_config_store import SystemConfigStore


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
    """Own persisted overrides and the process-active config snapshot."""

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

    def __init__(self, db: DB, registry: ConfigRegistry = CONFIG_REGISTRY):
        self.db = db
        self._store = SystemConfigStore(db)
        self._registry = registry
        self._definitions = dict(registry.by_key)
        self._defaults = registry.defaults()
        self._lock = threading.RLock()
        self._effective_values = dict(self._defaults)
        self._persisted_values = dict(self._defaults)

    def refresh(self, *, include_restart_required: bool = False) -> dict[str, object]:
        with self._lock:
            persisted = self._load_persisted_values_locked()
            effective = persisted if include_restart_required else dict(self._effective_values)
            for definition in self._registry.definitions:
                if include_restart_required or not definition.restart_required:
                    effective[definition.key] = persisted[definition.key]
            self._registry.validate_snapshot(effective)
            self._persisted_values = persisted
            self._effective_values = effective
            return dict(effective)

    def get(self, key: str, default: object | None = None) -> object:
        with self._lock:
            return self._effective_values.get(key, default)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._effective_values)

    def ui_sections(self) -> list[dict[str, object]]:
        with self._lock:
            effective = dict(self._effective_values)
            persisted = dict(self._persisted_values)
        buckets: dict[str, list[dict[str, object]]] = {}
        for definition in self._registry.definitions:
            buckets.setdefault(definition.category, []).append(
                self._config_row(definition, persisted, effective)
            )
        sections: list[dict[str, object]] = []
        for category, rows in sorted(
            buckets.items(),
            key=lambda item: (self._category_index(item[0]), item[0].lower()),
        ):
            rows.sort(key=lambda row: row["key"])
            sections.append(
                {
                    "category": category,
                    "slug": self.category_slug(category),
                    "rows": rows,
                    "count": len(rows),
                    "changed_count": sum(1 for row in rows if row["changed"]),
                }
            )
        return sections

    def section_by_slug(self, slug: str) -> dict[str, object] | None:
        safe_slug = self.category_slug(slug)
        return next(
            (section for section in self.ui_sections() if section["slug"] == safe_slug),
            None,
        )

    @staticmethod
    def category_slug(category: str) -> str:
        token = category.strip().lower()
        parts: list[str] = []
        current: list[str] = []
        for char in token:
            if char.isalnum():
                current.append(char)
            elif current:
                parts.append("".join(current))
                current = []
        if current:
            parts.append("".join(current))
        return "-".join(parts) or "misc"

    def validate_patch(self, payload: dict[str, object]) -> SystemConfigPatchPreview:
        with self._lock:
            before = dict(self._persisted_values)
        normalized: dict[str, object] = {}
        for payload_key, raw_value in payload.items():
            key = payload_key.strip()
            normalized[key] = self._registry.normalize(key, raw_value)
        after = dict(before)
        after.update(normalized)
        self._registry.validate_snapshot(after)
        diff_rows = self._diff_rows(before, after)
        return {
            "normalized": normalized,
            "diff": diff_rows,
            "changed": len(diff_rows),
            "before": before,
            "after": after,
        }

    def apply_patch(self, payload: dict[str, object], actor_user_id: int) -> dict[str, object]:
        with self._lock:
            preview = self.validate_patch(payload)
            after = dict(preview["after"])
            self._persist_overrides(after, actor_user_id)
            effective = self.refresh()
            diff_rows = self._diff_rows(dict(preview["before"]), after)
            persisted = dict(self._persisted_values)
        return {
            "changed": len(diff_rows),
            "diff": diff_rows,
            "effective": effective,
            "persisted": persisted,
        }

    def reset(self) -> dict[str, object]:
        with self._lock:
            self._store.clear_overrides()
            return self.refresh()

    def _category_index(self, category: str) -> int:
        if category in self._CATEGORY_ORDER:
            return self._CATEGORY_ORDER.index(category)
        return len(self._CATEGORY_ORDER) + 1

    def _config_row(
        self,
        definition: ConfigDefinition,
        persisted: dict[str, object],
        effective: dict[str, object],
    ) -> dict[str, object]:
        key = definition.key
        default_value = self._defaults[key]
        current_value = persisted[key]
        effective_value = effective[key]
        display = self._registry.display_value
        return {
            "key": key,
            "type": definition.kind.value,
            "category": definition.category,
            "description": definition.description,
            "min": definition.minimum,
            "max": definition.maximum,
            "restart_required": definition.restart_required,
            "impact": definition.impact,
            "choices": [str(item) for item in definition.choices],
            "default_value": default_value,
            "current_value": current_value,
            "effective_value": effective_value,
            "default_display": display(definition.kind, default_value),
            "current_display": display(definition.kind, current_value),
            "effective_display": display(definition.kind, effective_value),
            "changed": current_value != default_value,
            "pending_restart": bool(
                definition.restart_required and current_value != effective_value
            ),
            "input_name": f"config_{key}",
        }

    def _diff_rows(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        display = self._registry.display_value
        for definition in self._registry.definitions:
            key = definition.key
            previous = before.get(key, self._defaults[key])
            current = after.get(key, self._defaults[key])
            if previous == current:
                continue
            rows.append(
                {
                    "key": key,
                    "category": definition.category,
                    "type": definition.kind.value,
                    "restart_required": definition.restart_required,
                    "impact": definition.impact,
                    "before": previous,
                    "after": current,
                    "before_display": display(definition.kind, previous),
                    "after_display": display(definition.kind, current),
                }
            )
        return rows

    def _persist_overrides(self, values: dict[str, object], actor_user_id: int) -> None:
        self._store.replace_overrides(
            keys=list(self._definitions),
            values=values,
            defaults=self._defaults,
            actor_user_id=int(actor_user_id),
            updated_at=now_iso(),
        )

    def _load_persisted_values_locked(self) -> dict[str, object]:
        rows = self._store.override_rows()
        stale_keys = [row["key"] for row in rows if row["key"] not in self._definitions]
        if stale_keys:
            raise ValueError(
                "unknown persisted system config: " + ", ".join(sorted(stale_keys))
            )
        overrides: dict[str, object] = {}
        for row in rows:
            key = row["key"]
            if key not in self._definitions:
                continue
            raw_json = row["value_json"]
            try:
                parsed = json.loads(raw_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid persisted system config JSON: {key}") from exc
            try:
                normalized = self._registry.normalize(key, parsed)
            except ValueError as exc:
                raise ValueError(f"invalid persisted system config {key}: {exc}") from exc
            if normalized != self._defaults[key]:
                overrides[key] = normalized
        persisted = dict(self._defaults)
        persisted.update(overrides)
        self._registry.validate_snapshot(persisted)
        return persisted
