"""Prepare statement samples in an ephemeral Native package reader.

The immutable Native archive keeps authored sources and verified judge payloads
separate.  Statement consumers call this helper after extraction so generated
samples are rendered from the exact input and answer recorded by verification.
The persisted archive is never modified.
"""

from __future__ import annotations

from app.service.problem.test_spec import dumps_tests_spec, load_tests_spec
from app.service.problem_package.service import NativePackageReader


def hydrate_native_statement_samples(
    native: NativePackageReader,
    *,
    tests_spec_max_bytes: int,
) -> None:
    """Fill missing display samples from the Native manifest's verified payloads."""

    spec_path = native.root / "tests" / "spec.json"
    rows = load_tests_spec(spec_path, max_bytes=tests_spec_max_bytes)
    manifest_by_id = {row["id"]: row for row in native.manifest["tests"]}
    changed = False
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
            row["sample_input"] = input_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            changed = True
        if not row["sample_output"] and output_path is not None:
            row["sample_output"] = output_path.read_text(
                encoding="utf-8",
                errors="replace",
            )
            changed = True
    if changed:
        spec_path.write_text(
            dumps_tests_spec(rows, max_bytes=tests_spec_max_bytes),
            encoding="utf-8",
            newline="\n",
        )
