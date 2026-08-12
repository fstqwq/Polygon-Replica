# `app/service/judgehost`

This package adapts generic execution cases to the DOMjudge-compatible
Judgehost surface. It stages batches and cases, selects and leases work, serves
source, testcase, and executable files, records host/toolchain telemetry,
converts callbacks into structured execution results, and coordinates
cancellation.

Its inputs are prepared verification payloads, runtime source/input/answer
blobs, and authenticated `/api/v4/*` requests. Its outputs are Judgehost work and
file payloads plus decisions and late diagnostics published through one
injected execution port. The exact wire behavior belongs to the
[Judgehost protocol](../../../../protocol/judgehost.md).

Prepared work carries `verification_program_id`, the boundary name for
verification's `program_id`. The batch scheduler groups tests by
`(verification_id, verification_program_id)` and admits them only under one
stable program/compile definition. It keeps per-execution `run_id` and the
content-addressed `compile_key` separate; distinct programs can share a compile
cache entry without sharing program identity.

The package depends on workspace lookup, `RuntimeBlobStore`,
`RuntimeCacheIndex`, the injected `JudgehostExecutionPort`, and its process-local
task registry and batch scheduler. It does not import verification modules or
query verification tables. `CaseBinding` carries the opaque scope, program,
durable task, and test identities required for cache-payload lookup, exposure,
lease, completion, diagnostics, and cleanup. Host state, leases, toolchain
reports, executable/result cache indexes, and batch state do not persist across
process startup. Callback admission and per-case receipts linearize maintenance
and quiet cleanup.

The Judgehost composition root constructs one diagnostic publisher, completion
publisher, and batch finalizer, then injects those boundaries into callback and
dispatch orchestration. Final `add-judging-run` payloads are converted by the
dependency-light case normalizer. `artifact_capture.py` selects the bounded
callback files, validates historical pass bundles, and writes runtime cache
blobs without choosing a verdict or publishing a completion.
`diagnostic_payload.py` reduces already bounded debug and internal-error fields
to canonical diagnostic text without depending on persistence, runtime
composition, scheduling, or verification. `toolchain_versions.py` owns the
optional version-command handshake and process-local telemetry recording.

`result.py` retains callback receipts, lease-owner and generation validation,
claim ordering, normalization, durable publication, batch finalization, and
acknowledgement ordering. Scheduler/task-queue canonical helpers build compile-
failure and missing-case results because those paths have no final run callback;
the batch finalizer publishes and aggregates them.
Verification implements the execution port in its own package, where durable
task identity is checked before staged cases become fetchable and before lease
or diagnostic events are delivered. Completion, diagnostic, and lease
persistence remain outside the Judgehost package.
