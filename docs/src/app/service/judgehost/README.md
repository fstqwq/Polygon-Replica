# `app/service/judgehost`

This package adapts verification tasks to the DOMjudge-compatible Judgehost
surface. It stages batches and cases, selects and leases work, serves source,
testcase, and executable files, records host/toolchain telemetry, converts
callbacks into structured execution results, and coordinates cancellation.

Its inputs are prepared verification payloads, runtime source/input/answer
blobs, and authenticated `/api/v4/*` requests. Its outputs are Judgehost work and
file payloads plus results published through `VerificationTaskStore`. The exact
wire behavior belongs to the [Judgehost protocol](../../../../protocol/judgehost.md).

The package depends on SQLite-backed verification stores, workspace lookup,
`RuntimeBlobStore`, `RuntimeCacheIndex`, and the process-local task registry and
batch scheduler. Host state, leases, toolchain reports, executable/result cache
indexes, and batch state do not persist across process startup; terminal
verification summaries are persisted by the verification service. Result
processing remains multi-responsibility; PLC-006/007 track that refactor.
