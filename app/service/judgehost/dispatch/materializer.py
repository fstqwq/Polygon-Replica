from dataclasses import dataclass

from app.service.judgehost.batch.model import (
    CompileSubmission,
    ExecutionBatchSpec,
    ExecutionBatchRow,
)
from app.service.judgehost.cache.executable import ExecutableCache
from app.service.platform.runtime_blob_store import RuntimeBlobStore


@dataclass(frozen=True, slots=True)
class MaterializationRequest:
    batch: ExecutionBatchRow
    spec: ExecutionBatchSpec
    submission: CompileSubmission


@dataclass(frozen=True, slots=True)
class MaterializedBatch:
    submission: CompileSubmission


class BatchPayloadMaterializer:
    """Materialize one batch without owning or mutating batch lifecycle state."""

    def __init__(self, blobs: RuntimeBlobStore, executables: ExecutableCache) -> None:
        self._blobs = blobs
        self._executables = executables

    def materialize(self, request: MaterializationRequest) -> MaterializedBatch:
        batch = request.batch
        spec = request.spec
        submission = request.submission
        materialized_submission = CompileSubmission(
            compile_key=submission.compile_key,
            submit_id=submission.submit_id,
            source_name=submission.source_name,
            source_file=self._blobs.put_file(submission.source_file),
            extra_source_items=tuple(
                (name, self._blobs.put_file(payload))
                for name, payload in submission.extra_source_items
            ),
            compile_files=submission.compile_files,
        )
        self._executables.store(
            kind="run",
            executable_hash=batch["run_hash"],
            files=spec.run_files,
        )
        self._executables.store(
            kind="compare",
            executable_hash=batch["compare_hash"],
            files=spec.compare_files,
        )
        self._executables.store(
            kind="compile",
            executable_hash=batch["compile_hash"],
            files=submission.compile_files,
        )
        return MaterializedBatch(submission=materialized_submission)
