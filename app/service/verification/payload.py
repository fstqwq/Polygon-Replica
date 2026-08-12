from pathlib import Path

from app.service.platform.runtime_blob_store import PayloadFile


def answer_name(test_name: str) -> str:
    return f"{Path(test_name).stem}.ans"


def test_payload_entry(
    *,
    test_name: str,
    input_file: PayloadFile,
    answer_file: PayloadFile,
) -> dict[str, object]:
    return {
        "name": test_name,
        "input_file": input_file.to_payload(),
        "answer_name": answer_name(test_name),
        "answer_file": answer_file.to_payload(),
    }


def prepared_payload_for_uploaded_source(
    *,
    source_label: str,
    run_id: str,
    test_name: str,
    input_file: PayloadFile,
    answer_file: PayloadFile,
    verification_payload_base: dict[str, object],
    extra_source_files: dict[str, PayloadFile] | None = None,
    manual_validate_only: bool = False,
) -> dict[str, object]:
    verification_payload = dict(verification_payload_base)
    verification_payload["tests"] = [
        test_payload_entry(test_name=test_name, input_file=input_file, answer_file=answer_file)
    ]
    prepared: dict[str, object] = {
        "run_id": run_id,
        "verification_payload": verification_payload,
        "source_label": source_label,
    }
    if extra_source_files:
        prepared["extra_source_files"] = {
            name: payload.to_payload()
            for name, payload in extra_source_files.items()
        }
    if manual_validate_only:
        prepared["manual_validate_only"] = True
    return prepared
