from __future__ import annotations

import json


def cap_summary_list_field(
    payload: dict,
    field: str,
    limit: int,
    truncated_key: str,
    total_key: str,
    limit_key: str,
) -> None:
    values = payload.get(field)
    if values is None:
        return
    cap = max(1, int(limit))
    total = len(values)
    payload[limit_key] = cap
    payload[total_key] = total
    if total > cap:
        payload[field] = values[:cap]
        payload[truncated_key] = True
        return
    payload[truncated_key] = False


def summary_for_db(summary: dict, *, normalize_diagnostics_for_db, diagnostics_limit: int) -> str:
    payload = dict(summary)
    cap_summary_list_field(
        payload,
        "diagnostics",
        diagnostics_limit,
        "diagnostics_truncated",
        "diagnostics_total",
        "diagnostics_limit",
    )
    diagnostics = payload.get("diagnostics")
    if diagnostics is not None:
        payload["diagnostics"] = normalize_diagnostics_for_db(diagnostics, diagnostics_limit)
    return json.dumps(payload)
