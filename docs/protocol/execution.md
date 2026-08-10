# Execution and verification protocol

## Job model

Preview compilation is synchronous in the HTTP request. Verification, custom
run, export, and contest build jobs are submitted to one bounded process-local
worker queue. Queue records and `worker-queue-events.jsonl` are runtime
diagnostics; they are cleared at startup and do not recover work after restart.
Durable job summary rows are reconciled to failed or cancelled states during
startup.

A custom run is represented as a verification with the custom kind. It uses the
same task storage, Judgehost dispatch, results, and artifact model rather than a
second run domain.

## Verification DAG

Verification records the selected tests and solution/source paths, then stores
tasks in `verification_tasks`. Verification kinds are `all`, `sample`, and
`custom`; their durable statuses are `queued`, `pending`, `running`, `ok`, and
`failed`. A task identifies its kind, source, logical run, test, expected
behavior, optional predecessor, final status, and serialized execution result.

The graph has exactly three task kinds:

- `generate-input` materializes one testcase input. Manual tests use a trivial
  program over the authored input payload; generated tests execute the selected
  generator.
- `main-correct` runs the accepted solution after that test's input is ready.
- `solution-run` runs another solution after the same test's `main-correct`
  task succeeds.

Task statuses are `pending`, `queued`, `leased`, `done`, `failed`, and
`cancelled`; `leased` is displayed as running by read models.

Thus accepted-solution execution gates the other expected-behavior checks per
test. Identical generator invocations share one generated result; duplicate
generation tasks depend on the owning invocation and finish as skipped.

Generator-backed tests execute their generator payload, including parameters.
The configured validator is the generator task's checker. There is no additional
validator task kind between generation and solution execution.

The DAG scheduler publishes runnable tasks in bounded batches, polls Judgehost
case-cache misses, and advances dependants when predecessor events arrive.
Predecessor failure or cancellation skips or cancels dependants. Compile-only,
pass-fail, interactive, and multi-pass execution are mapped to Judgehost batches
and case results by the verification and Judgehost services.

Verification planning preserves the canonical authored memory limit. Judgehost
dispatch converts it exactly from MiB to KiB by multiplying by 1024; it does not
apply a separate execution minimum. An internal dispatch payload containing a
non-integer memory limit or a value below 1 is invalid and fails before work is
sent to a host.

## Identity and cache

Verification staleness/signature input consists of `config/problem.json`,
`config/build.json`, `tests/spec.json`, and regular files below `generators/`,
`validators/`, `checkers/`, `interactors/`, `solutions/`, `tests/manual/`,
`tests/generator/`, and `third_party/testlib/`. Missing roots and files are also
represented in the signature. Statement-only changes do not stale a
verification.

Each dispatched execution then uses content-addressed source, extra-source, and
input payloads plus its canonical run configuration. Generator parameters are
part of the generator input payload and therefore its invocation and cache
identity. Judgehost compiler and runner versions are recorded as telemetry
only; they are not currently cache-key fields or consistency gates.

An available cached result may be reused only for its matching identity. Cache
availability is checked separately from durable verification status.

## Results and artifacts

Task results are serialized in `verification_tasks.result_json`. The canonical
shape has `outcome`, `compile`, ordered `passes`, and `warnings`. Each pass
records its number, capture status, run result, verdict, score, answer flag,
resource usage, feedback, and artifact locators. Pass numbers are contiguous
from one; an output locator and transcript locator are mutually exclusive.

Per-test generated input and answer locators are stored in
`verification_artifact_refs.input_ref` and `answer_ref`. Output, transcript,
diff, metadata, and feedback locators are carried by serialized execution/pass
results; there is no physical `verification_tasks.output_ref` column.

Locators point into cleanup-safe runtime storage. A durable terminal result may
remain after cleanup while one or more payloads are unavailable. Downloads MUST
resolve the locator through the owning store and fail as unavailable when the
payload no longer exists. Verification detail downloads use the existing
`/problems/{problem:path}/artifacts/{verification_id}/{rel_path:path}` route;
no `/runs/{run_id}/artifacts/...` route exists.
