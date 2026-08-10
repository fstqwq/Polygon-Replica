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
tasks in `verification_tasks`. A task identifies its kind, source, logical run,
test, expected behavior, optional predecessor, final status, and serialized
execution result.

Generator-backed tests execute their generator payload, including parameters.
The configured validator is the generator task's checker. There is no additional
validator task kind between generation and solution execution.

The DAG is scheduled only when predecessors have reached the required terminal
state. Compile-only, pass-fail, interactive, and multi-pass execution are mapped
to Judgehost batches and case results by the verification and Judgehost
services.

Verification planning preserves the canonical authored memory limit. Judgehost
dispatch converts it exactly from MiB to KiB by multiplying by 1024; it does not
apply a separate execution minimum. An internal dispatch payload containing a
non-integer memory limit or a value below 1 is invalid and fails before work is
sent to a host.

## Identity and cache

Execution identity includes canonical source and configuration inputs needed to
reproduce the work. Generator parameters are already part of the generator
payload and therefore its identity. Judgehost compiler and runner versions are
recorded as telemetry only; they are not currently cache-key fields or
consistency gates.

An available cached result may be reused only for its matching identity. Cache
availability is checked separately from durable verification status.

## Results and artifacts

Task results are serialized in `verification_tasks.result_json`.
Per-test generated input and answer locators are stored in
`verification_artifact_refs.input_ref` and `answer_ref`. Output, transcript,
diff, metadata, and feedback locators are carried by serialized execution/pass
results; there is no physical `verification_tasks.output_ref` column.

Locators point into cleanup-safe runtime storage. A durable terminal result may
remain after cleanup while one or more payloads are unavailable. Downloads MUST
resolve the locator through the owning store and fail as unavailable when the
payload no longer exists. Verification detail downloads use the existing
problem-scoped `/artifacts/{verification_id}/...` routes; no
`/runs/{run_id}/artifacts/...` route exists.
