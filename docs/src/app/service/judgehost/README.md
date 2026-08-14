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
verification's `program_id`. The batch runtime groups tests by
`(verification_id, verification_program_id)` and admits them only under one
stable program/compile definition. It keeps per-execution `run_id` and the
content-addressed `compile_key` separate; distinct programs can share a compile
cache entry without sharing program identity.

The package depends on workspace lookup, `RuntimeBlobStore`,
`RuntimeCacheIndex`, the injected `JudgehostExecutionPort`, and its process-local
task registry and batch runtime. It does not import verification modules or
query verification tables. `CaseBinding` carries the opaque scope, program,
durable task, and test identities required for cache-payload lookup, exposure,
lease, completion, diagnostics, and cleanup. Host state, leases, toolchain
reports, executable/result cache indexes, and batch state do not persist across
process startup. Callback admission and per-case receipts linearize maintenance
and quiet cleanup.

`JudgehostBatchRuntime` owns one canonical `BatchState` and composes admission,
dispatch, completion, finalization, and maintenance capabilities around the
same re-entrant lock. Canonical batch/case records, leases, callback receipts,
materialization and finalization claims, retries, and derived ready indexes
therefore transition under one
linearization boundary. A scheduling policy receives an immutable candidate
snapshot and returns a decision; it never owns canonical state or leases.

The other process-local owners are `JudgehostTaskRegistry`, for task identity,
lifecycle, indexes, and wait generation, and `JudgehostHostRegistry`, for host
presence, enablement, peer telemetry, and toolchain reports. The public
`Judgehost` facade constructs these owners directly. No context bag exposes
their mutable storage.

Dependencies flow in one direction: these resource owners are consumed by
typed cache, storage, protocol, and execution-port adapters; independent
admission, dispatch, callback, finalization, query, and maintenance use cases
consume only the owners and adapters they need; the public `Judgehost` facade
sequences their typed outcomes. A use case does not call a sibling use case.

The package layout follows those responsibilities instead of keeping the
implementation flat:

- `batch/` owns the aggregate, canonical state, atomic capabilities, immutable
  model snapshots, ready index, and pure scheduling policies.
- `task/` owns payload preparation, task admission, task identity, read-only
  result projection and queries, retention, and the atomic handoff
  into batch topology. No task lifecycle object combines these operations.
- `dispatch/` owns cache probing, payload materialization, host selection, and
  typed lease claim/commit/abort.
- `finalization/` owns durable case publication, task terminalization, batch
  finalization claims, and retry.
- `maintenance/` owns cancellation, terminal cleanup, retention, and
  process-local reset coordination.
- `host/` owns host presence, the toolchain-version callback handshake,
  telemetry collection, and the public host-status projection without exposing
  its registry.
- `cache/` owns executable and case-result cache storage contracts.
- `callback/` owns execution-result and diagnostic callback parsing, bounded
  artifact capture, pass bundles, result normalization, and transcripts.
- `ports/` defines the typed binding, lease, diagnostic, and completion ports
  supplied by verification.
- `domjudge/` owns protocol codecs and projections, DOMjudge numeric IDs and
  executable identities, result interpretation, source/testcase/executable
  file streaming, the
  compile/run script catalog, canonical case-result conversion, and the
  executable scripts served to Judgehost. Generic hashing and text
  normalization do not acquire DOMjudge-specific wrappers; untyped protocol
  fields are decoded once and canonical internal records are consumed directly.
The top-level `api.py` is the composition and public service boundary. It
constructs owners, adapters, and independent use cases and sequences their
typed outcomes. Dispatch and callback ingestion do not invoke finalization;
the facade completes finalization before returning the existing wire response.
Final `add-judging-run` payloads are converted by the
dependency-light case normalizer. `callback/artifact_capture.py` selects the
bounded callback files, validates historical pass bundles, and writes runtime
cache blobs without choosing a verdict or publishing a completion.
`callback/diagnostic_payload.py` reduces already bounded debug and
internal-error fields to canonical diagnostic text without depending on
persistence, runtime composition, scheduling, or verification.
`host/toolchain_versions.py` owns the optional version-command handshake
and process-local telemetry recording.

`callback/result.py` retains callback receipts, lease-owner and generation
validation, claim ordering, normalization, and canonical result commit. It
returns terminal batch and touched-verification identities to the facade
instead of calling finalization, durable publication, or quiet cleanup. The
facade finalizes durable work before refreshing the cleanup deadline and
returning the protocol acknowledgement. Runtime state is claimed under the
batch lock; filesystem
and cache work occurs after that lock is released; the result is then committed
or aborted against the claim generation. Task query and terminalization use
cases build
compile-failure and missing-case results because those paths have no final run
callback; the batch finalizer publishes and aggregates them. Quiet cleanup also
rejects a batch with an active finalization claim, so it cannot remove state
while durable publication is outside the lock.
Verification implements the execution port in its own package, where durable
task identity is checked before staged cases become fetchable and before lease
or diagnostic events are delivered. Completion, diagnostic, and lease
persistence remain outside the Judgehost package.
