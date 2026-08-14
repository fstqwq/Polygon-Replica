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

`JudgehostBatchRuntime` is the only batch-state boundary exposed by
`JudgehostState`. It owns one canonical `BatchState` and composes admission,
dispatch, completion, finalization, and maintenance capabilities around the
same re-entrant lock. Canonical batch/case records, leases, callback receipts,
finalization retries, and derived ready indexes therefore transition under one
linearization boundary. A scheduling policy receives an immutable candidate
snapshot and returns a decision; it never owns canonical state or leases.

The package layout follows those responsibilities instead of keeping the
implementation flat:

- `batch/` owns the aggregate, canonical state, atomic capabilities, immutable
  model snapshots, ready index, and pure scheduling policies.
- `work/` owns enqueue, dispatch payload preparation, cache probing,
  publication, finalization orchestration, task history, and terminal cleanup.
- `callback/` owns callback parsing, bounded artifact capture, pass bundles,
  result normalization, transcripts, and canonical execution-result helpers.
- `ports/` defines the typed binding, lease, diagnostic, and completion ports
  supplied by verification.
- `domjudge/` owns protocol codecs and projections, DOMjudge numeric IDs and
  executable identities, result interpretation, file streaming, the
  compile/run toolkit, and the executable scripts served to Judgehost. Generic
  hashing and text normalization do not acquire DOMjudge-specific wrappers;
  untyped protocol fields are decoded once and canonical internal records are
  consumed directly.
- `telemetry/` records toolchain reports and produces the public host-status
  projection.

The top-level `api.py`, `core.py`, and `state.py` are the composition and public
service boundary. They construct one diagnostic publisher, completion
publisher, and batch finalizer, then inject those boundaries into callback and
dispatch orchestration. Final `add-judging-run` payloads are converted by the
dependency-light case normalizer. `callback/artifact_capture.py` selects the
bounded callback files, validates historical pass bundles, and writes runtime
cache blobs without choosing a verdict or publishing a completion.
`callback/diagnostic_payload.py` reduces already bounded debug and
internal-error fields to canonical diagnostic text without depending on
persistence, runtime composition, scheduling, or verification.
`telemetry/toolchain_versions.py` owns the optional version-command handshake
and process-local telemetry recording.

`callback/result.py` retains callback receipts, lease-owner and generation
validation, claim ordering, normalization, durable publication, batch
finalization, and acknowledgement ordering. Runtime state is claimed under the
batch lock; SQLite, filesystem, cache, and execution-port work occurs after
that lock is released; the result is then committed or aborted against the
claim generation. Runtime and task-queue canonical helpers build
compile-failure and missing-case results because those paths have no final run
callback; the batch finalizer publishes and aggregates them. Quiet cleanup also
rejects a batch with an active finalization claim, so it cannot remove state
while durable publication is outside the lock.
Verification implements the execution port in its own package, where durable
task identity is checked before staged cases become fetchable and before lease
or diagnostic events are delivered. Completion, diagnostic, and lease
persistence remain outside the Judgehost package.
