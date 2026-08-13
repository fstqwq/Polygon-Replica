"""Prepare statement samples in an ephemeral verified revision reader.

The verified revision keeps authored sources and verified judge payloads
separate.  Statement consumers call this helper after extraction so generated
samples are rendered from the exact input and answer recorded by verification.
The persisted archive is never modified.
"""

from app.service.problem.test_spec import (
    dumps_tests_spec,
    load_tests_spec,
    read_statement_sample_text,
)
from app.service.problem_package.service import VerifiedRevisionReader


def hydrate_verified_statement_samples(
    revision: VerifiedRevisionReader,
    *,
    tests_spec_max_bytes: int,
    statement_sample_max_bytes: int,
) -> None:
    """Fill missing display samples from the verified judge payloads."""

    if statement_sample_max_bytes <= 0:
        raise ValueError("statement sample display byte limit must be positive")

    spec_path = revision.root / "tests" / "spec.json"
    rows = load_tests_spec(
        spec_path,
        document_max_bytes=tests_spec_max_bytes,
        sample_max_bytes=statement_sample_max_bytes,
    )
    manifest_by_id = {row["id"]: row for row in revision.manifest["tests"]}
    changed = False
    for row in rows:
        if not row["sample"]:
            continue
        materialized = manifest_by_id.get(str(row["id"]))
        if materialized is None:
            raise ValueError(f"verified revision is missing test: {row['id']}")
        input_path = revision.payload(materialized, "sample_input") or revision.payload(
            materialized,
            "input",
        )
        output_path = revision.payload(materialized, "sample_output") or revision.payload(
            materialized,
            "answer",
        )
        if not row["sample_input"] and input_path is not None:
            sample_input = read_statement_sample_text(
                input_path,
                max_bytes=(
                    statement_sample_max_bytes
                    - len(row["sample_output"].encode("utf-8"))
                ),
            )
            row["sample_input"] = sample_input
            changed = True
        if not row["sample_output"] and output_path is not None:
            sample_output = read_statement_sample_text(
                output_path,
                max_bytes=(
                    statement_sample_max_bytes
                    - len(row["sample_input"].encode("utf-8"))
                ),
            )
            row["sample_output"] = sample_output
            changed = True
    if changed:
        spec_path.write_text(
            dumps_tests_spec(
                rows,
                document_max_bytes=tests_spec_max_bytes,
                sample_max_bytes=statement_sample_max_bytes,
            ),
            encoding="utf-8",
            newline="\n",
        )
