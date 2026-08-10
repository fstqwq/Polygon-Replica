# `app/service/judgehost`

This package adapts verification tasks to the DOMjudge-compatible Judgehost
surface. It stages batches and cases, selects and leases work, serves source,
testcase, and executable files, records host/toolchain telemetry, converts
callbacks into structured execution results, and coordinates cancellation.

Its inputs are prepared verification payloads, runtime source/input/answer
blobs, and authenticated `/api/v4/*` requests. Its outputs are Judgehost work and
file payloads plus decisions and late diagnostics published through narrow
verification-owned sinks. The exact wire behavior belongs to the
[Judgehost protocol](../../../../protocol/judgehost.md).

Prepared work carries `verification_program_id`, the boundary name for
verification's `program_id`. The batch scheduler groups tests by
`(verification_id, verification_program_id)` and admits them only under one
stable program/compile definition. It keeps per-execution `run_id` and the
content-addressed `compile_key` separate; distinct programs can share a compile
cache entry without sharing program identity.

The package depends on SQLite-backed verification stores, workspace lookup,
`RuntimeBlobStore`, `RuntimeCacheIndex`, and the process-local task registry and
batch scheduler. Host state, leases, toolchain reports, executable/result cache
indexes, and batch state do not persist across process startup; terminal
verification summaries and accepted late-diagnostic snapshots are persisted by
the verification service. Callback admission and per-case receipts linearize
maintenance and quiet cleanup. Result processing remains multi-responsibility;
PLC-006/007 track that refactor.
