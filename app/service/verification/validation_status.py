from typing import cast


def _parse_step_status(raw: object) -> str:
    """Return a stored verification step status token."""

    status = cast(str | None, raw)
    if status is None:
        return ""
    return status


def build_validation_status(verification_row: dict[str, object] | None) -> str:
    """Return the export-facing validation label for a verification record."""

    if verification_row is None:
        return "validation unknown"
    status = cast(str | None, verification_row.get("status"))
    if status is None:
        status = ""
    details = dict(cast(dict[str, object], verification_row.get("details") or {}))
    sanity_status = _parse_step_status(details.get("sanity_status"))
    validation_status = _parse_step_status(details.get("validation_status"))
    if sanity_status == "failed" or validation_status == "failed":
        return "validation failed"
    if sanity_status in {"passed", "warning"} or validation_status in {"passed", "warning"}:
        return "validation passed"
    if sanity_status == "unknown":
        return "validation unknown"
    if validation_status == "unknown":
        return "validation unknown"
    failed_step = _parse_step_status(details.get("failed_step"))
    if failed_step in {"validate", "sanity"}:
        return "validation failed"
    if status == "ok":
        return "validation passed"
    return "validation unknown"
