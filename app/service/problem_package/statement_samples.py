"""Prepare statement samples in an ephemeral Native package reader.

The immutable Native archive keeps authored sources and verified judge payloads
separate.  Statement consumers call this helper after extraction so generated
samples are rendered from the exact input and answer recorded by verification.
The persisted archive is never modified.
"""

from __future__ import annotations

from pathlib import Path

from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.problem_package.service import NativePackageReader


def _bounded_display_text(path: Path, *, remaining_bytes: int) -> tuple[str, int]:
    """Read one display payload without exceeding the remaining aggregate budget."""

    if remaining_bytes < 0:
        raise ValueError("generated statement samples exceed display byte limit")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native statement sample payload is not a regular file")
    try:
        declared_size = path.stat().st_size
    except OSError as exc:
        raise ValueError("Native statement sample payload is unavailable") from exc
    if declared_size > remaining_bytes:
        raise ValueError("generated statement samples exceed display byte limit")
    try:
        with path.open("rb") as source:
            payload = source.read(remaining_bytes + 1)
    except OSError as exc:
        raise ValueError("Native statement sample payload is unavailable") from exc
    if len(payload) > remaining_bytes:
        raise ValueError("generated statement samples exceed display byte limit")
    text = payload.decode("utf-8", errors="replace")
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > remaining_bytes:
        raise ValueError("generated statement samples exceed display byte limit")
    return text, encoded_size


def hydrate_native_statement_samples(
    native: NativePackageReader,
    *,
    tests_spec_max_bytes: int,
) -> None:
    """Fill missing display samples from the Native manifest's verified payloads."""

    if tests_spec_max_bytes <= 0:
        raise ValueError("statement sample display byte limit must be positive")

    spec_path = native.root / "tests" / "spec.json"
    rows = load_tests_spec(spec_path, max_bytes=tests_spec_max_bytes)
    manifest_by_id = {row["id"]: row for row in native.manifest["tests"]}
    changed = False
    generated_bytes = 0
    for row in rows:
        if not bool(row["sample"]):
            continue
        materialized = manifest_by_id.get(str(row["id"]))
        if materialized is None:
            raise ValueError(f"Native manifest is missing test: {row['id']}")
        input_path = native.payload(materialized, "sample_input") or native.payload(
            materialized,
            "input",
        )
        output_path = native.payload(materialized, "sample_output") or native.payload(
            materialized,
            "answer",
        )
        if not row["sample_input"] and input_path is not None:
            sample_input, consumed = _bounded_display_text(
                input_path,
                remaining_bytes=tests_spec_max_bytes - generated_bytes,
            )
            row["sample_input"] = sample_input
            generated_bytes += consumed
            changed = True
        if not row["sample_output"] and output_path is not None:
            sample_output, consumed = _bounded_display_text(
                output_path,
                remaining_bytes=tests_spec_max_bytes - generated_bytes,
            )
            row["sample_output"] = sample_output
            generated_bytes += consumed
            changed = True
    if changed:
        spec_path.write_text(
            dumps_tests_spec(rows, max_bytes=tests_spec_max_bytes),
            encoding="utf-8",
            newline="\n",
        )
