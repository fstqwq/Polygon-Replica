"""Strict JSON decoding shared by authored problem-source codecs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import NoReturn


class _DuplicateKey(ValueError):
    pass


def _object_from_pairs(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateKey(key)
        payload[key] = value
    return payload


def _invalid(label: str, message: str) -> NoReturn:
    raise ValueError(f"{label}: {message}")


def loads_object(text: str, *, label: str) -> dict[str, object]:
    """Decode one non-empty JSON object while rejecting duplicate keys."""

    if not isinstance(text, str):
        _invalid(label, "content must be UTF-8 text")
    if not text.strip():
        _invalid(label, "file is empty")
    try:
        payload = json.loads(text, object_pairs_hook=_object_from_pairs)
    except _DuplicateKey as exc:
        _invalid(label, f"duplicate key '{exc.args[0]}'")
    except json.JSONDecodeError as exc:
        _invalid(label, f"invalid JSON at line {exc.lineno} column {exc.colno}")
    if not isinstance(payload, dict):
        _invalid(label, "must be an object")
    return payload


def reject_unknown_keys(
    payload: dict[str, object],
    *,
    allowed: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(payload).difference(allowed))
    if unknown:
        _invalid(label, f"unsupported key '{unknown[0]}'")


def require_keys(
    payload: dict[str, object],
    *,
    required: frozenset[str],
    label: str,
) -> None:
    missing = sorted(required.difference(payload))
    if missing:
        _invalid(label, f"missing key '{missing[0]}'")
