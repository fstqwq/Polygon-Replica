from __future__ import annotations

import base64
from pathlib import Path


def answer_name(test_name: str) -> str:
    return f"{Path(test_name).stem}.ans"


def test_payload_entry(
    *,
    test_name: str,
    input_bytes: bytes,
    answer_bytes: bytes,
) -> dict[str, str]:
    return {
        "name": test_name,
        "input_b64": base64.b64encode(input_bytes).decode("ascii"),
        "answer_name": answer_name(test_name),
        "answer_b64": base64.b64encode(answer_bytes).decode("ascii"),
    }


def prepared_payload_for_uploaded_source(
    *,
    source_label: str,
    run_id: str,
    test_name: str,
    input_bytes: bytes,
    answer_bytes: bytes,
    verification_payload_base: dict[str, object],
    extra_sources_b64: dict[str, str] | None = None,
    manual_validate_only: bool = False,
) -> dict[str, object]:
    verification_payload = dict(verification_payload_base)
    verification_payload["tests"] = [
        test_payload_entry(test_name=test_name, input_bytes=input_bytes, answer_bytes=answer_bytes)
    ]
    prepared: dict[str, object] = {
        "run_id": run_id,
        "verification_payload": verification_payload,
        "source_label": source_label,
    }
    if extra_sources_b64:
        prepared["extra_sources_b64"] = dict(extra_sources_b64)
    if manual_validate_only:
        prepared["manual_validate_only"] = True
    return prepared
