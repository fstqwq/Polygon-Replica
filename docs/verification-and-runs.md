# Verification, Runs, and Judgehost Integration

## Two User-Facing Execution Modes

| Mode | Purpose | Current implementation |
|------|---------|------------------------|
| Verification | full problem check | creates a `verifications` row and a task graph in `verification_tasks` |
| Run | ad-hoc execution from the run page | uses the same verification/task-graph pipeline, typically with `kind=custom` |

Both modes delegate code execution to the judgehost adapter. The application itself does not run user code directly on the web process.

## Current Verification Flow

```mermaid
sequenceDiagram
    participant UI as Browser
    participant Impl as impl/
    participant VS as VerificationService
    participant WQ as WorkerQueue
    participant DAG as verification_dag
    participant JH as Judgehost adapter
    participant JD as judgedaemon
    participant DB as SQLite

    UI->>Impl: start verification or run
    Impl->>VS: begin verification record
    Impl->>WQ: enqueue verification worker job
    WQ->>DAG: run verification task graph
    DAG->>DB: insert verification_tasks rows
    DAG->>JH: enqueue ready tasks
    JD->>JH: fetch-work
    JH-->>JD: compile/run payload
    JD->>JH: update-judging / add-judging-run
    JH->>DB: update verification task rows
    JH->>VS: persist artifact refs
```

## Current Task Graph Model

A verification is represented by:
- one row in `verifications`
- many rows in `verification_tasks`

Important task kinds in the current graph:
- `generate-input`
- `main-correct`
- `solution-run`

Each task row records:
- `task_kind`
- `source_path`
- `logical_run_id`
- `test_name`
- `expected_behavior`
- final state and verdict
- timing and memory
- `compile_log`, `diagnostics_json`, `error_text`, `feedback_text`
- `output_ref`

`logical_run_id` is the column/grouping token used by the run-detail UI. It is not a blob signature.

## Verification Kinds

Current durable values are:
- `all`
- `sample`
- `custom`

Meaning:
- `all`: full test set
- `sample`: sample-only verification
- `custom`: user-selected subset from the run page

## Status Model

Top-level verification statuses currently used in the web UI are:
- `queued`
- `pending`
- `running`
- `ok`
- `failed`

There is no top-level durable `cancelled` status anymore. User cancellation is recorded as `failed` with `fail_reason = verification cancelled by user`.

## Artifact Model

Current verification results are ref-based.

### Stored in SQLite
- `verifications`
- `verification_tasks.output_ref`
- `verification_artifact_refs.input_ref`
- `verification_artifact_refs.answer_ref`

### Stored in the blob store
Blob payloads live in `judge-fs-index` and are addressed by `cache://...` tokens.

Current roles:
- testcase input/answer cache
- exact case-cache payloads
- verification artifact blobs created from `input_ref`, `answer_ref`, and non-cache `output_ref`

## Download and Preview Paths

Fresh run and verification detail pages use only `/problems/{problem:path}/artifacts/{verification_id}/...`.

Current paths:
- `tests/{test_name}` -> resolves `input_ref`
- `ans/{answer_name}` -> resolves `answer_ref`
- `output/{task_id}/{file_name}` -> resolves `verification_tasks.output_ref`
- `blob/{encoded-token}/{file_name}` -> resolves an explicit blob token

There is no fresh `/runs/{run_id}/artifacts/...` path anymore.

## Input, Answer, and Output Lifecycle

### `solution-run`
- judgehost executes the case in `runtime/judgehost-runs/<judgehost_task_id>/...`
- the result becomes an exact case-cache blob when eligible
- `verification_tasks.output_ref` stores the locator
- the detail page reads output through `output_ref`

### `generate-input`
- output bytes are resolved from the execution result
- verification stores an `input_ref` for the test in `verification_artifact_refs`
- the detail page and preview sample sync read input through `input_ref`

### `main-correct`
- main runs through compare/checker; it does not skip compare
- on success, verification stores an `answer_ref` in `verification_artifact_refs`
- the detail page and preview sample sync read answer through `answer_ref`

## Judgehost Integration

The judgehost adapter exposes the DOMjudge-compatible API at `/api/v4/*`.

Current important endpoints:
- `GET /api/v4/config`
- `GET /api/v4/languages`
- `GET /api/v4/judgehosts`
- `POST /api/v4/judgehosts`
- `POST /api/v4/judgehosts/fetch-work`
- `GET /api/v4/judgehosts/get_files/source/{item_id}`
- `GET /api/v4/judgehosts/get_files/source/{contest_id}/{item_id}`
- `GET /api/v4/judgehosts/get_files/{file_type}/{item_id}`
- `GET /api/v4/judgehosts/get_version_commands/{judgetask_id}`
- `PUT /api/v4/judgehosts/check_versions/{judgetask_id}`
- `PUT /api/v4/judgehosts/update-judging/{hostname}/{judgetask_id}`
- `POST /api/v4/judgehosts/add-judging-run/{hostname}/{judgetask_id}`
- `POST /api/v4/judgehosts/add-debug-info/{hostname}/{judgetask_id}`
- `POST /api/v4/judgehosts/internal-error`

Authentication is separate from session auth and uses judgehost credentials.

## Judgehost Lifecycle

The runtime keeps four distinct lifecycle layers:

- `verification_tasks` rows are durable per-case product results. A case report updates its matching row immediately and only once.
- A judgehost task has an immutable case set. It becomes terminal only after all of its own cases are `reported` or `cancelled`.
- A DOMjudge case is leased independently. Explicitly disabling a host releases
  its `leased` cases back to `pending`; result and cache I/O first claim the Case
  as `reporting` or `cache-probing`, then commit a terminal result.
- A DOMjudge job is a temporary execution batch. Grouped jobs may contain several tasks, but they are not verification boundaries.

Cancellation moves idle cases directly to `cancelled`. An in-flight
`reporting/cache-probing` Case records a deferred cancellation, which takes effect
when the current claim commits or aborts. Late judgedaemon callbacks are
acknowledged without reviving the case.

The last terminal Case atomically changes its Job from `open` to
`finalize-pending` and removes it from the appendable group index. Exactly one
finalizer can claim `finalize-pending -> finalizing`; failure returns the Job to
`finalize-pending` for indexed retry. A task arriving after closure receives a
new rolling grouped job instead of reopening the old one.

Later judgehost polls service the finalization retry heap. Duplicate result
callbacks are idempotent after the first reporting claim. A transient publication
failure therefore retains the work root instead of stranding an unresumable job.

Case publication, task result aggregation, and task-terminal notification finish
before work-root removal and the final `completed/failed` transition.
Executable scripts have a different lifetime: they remain runtime-scoped and are
cleared at service startup, not when an individual job or verification finishes.

The verification DAG uses a preordered ready deque and processes every dependency
edge once. The judgehost Task Registry stores only identity, immutable request
fingerprints, result receipts, wait conditions, and terminal cleanup metadata; it
does not schedule work.

Execution scheduling uses one global ready-Job heap plus one cache-pending heap and
one runnable Case heap per Job. Heap entries carry generations and are rebuilt only
when their local stale ratio crosses a threshold. Fetch does not scan or sort all
Tasks, Jobs, or Cases. One ordinary reentrant lock protects only in-memory
dictionaries, counters, sets, and heaps; cache, filesystem, SQLite, and Coordinator
notifications are never accessed while it is held. Executable callbacks use a
lifecycle-maintained script-ID index rather than scanning open Jobs.

Cases enter a Job as `staged`, atomically activate as `cache-pending`, and cannot be
leased until their exact result-cache probe misses. A full cache hit reaches
`reported` without creating a work root. Source files and executable scripts are
materialized lazily only after at least one miss. Before compilation succeeds, one
smallest-ordinal Case is the compile leader; successful compilation opens the
remaining Cases to other hosts.

Every reported Case owns one immutable canonical `CaseResult`. Cache hits and cold
callbacks build the same object, including feedback and the verification test row.
The Scheduler decrements a per-Task remaining counter in constant time; only the
last Case sorts that Task's Cases once and writes its final summary. Case polling is
an indexed lookup and never reconstructs feedback from artifacts.

JudgeFS entries are synchronized per `(kind, key, signature)` and published from a
temporary directory. Unrelated cache keys perform file I/O concurrently; the small
global mutex only maintains ref-counted key locks. Fetch probes cache Cases with a
fixed Case budget, a monotonic-time budget, and bounded claim chunks.

Only two service classes exist: foreground direct/compile-only work and background
verification work. Foreground work cooperatively preempts between fetch batches;
already leased Cases are never cancelled. Within a Job, ready Cases are leased by
numeric test ordinal. Across Jobs, the scheduler prefers service class, earliest
verification scope, next test ordinal, and admission order while retaining active
Job affinity.

There is no automatic Case lease timeout. Judgehost registration is also a
heartbeat and therefore cannot safely distinguish a restart from a live daemon.
Operators must disable an unresponsive host to return its Cases to `pending` before
re-enabling or replacing it. This avoids duplicate delivery caused by treating
periodic registration as reconnect recovery.

Job and Case state is held in typed indexed memory records rather than a shared
in-memory SQLite connection. Terminal verification results are persisted through
the existing Coordinator batch path before successors are released.

After a verification's final detail and status are durable, one process-wide
deadline scheduler starts a 60-second quiet window. Late result, internal-error,
debug-info, or lease-return activity restarts that verification's window. At the
deadline only that verification's indexed terminal task/case identities are
removed; a shared job remains until its final owner is quiet. Unknown callbacks
after cleanup are acknowledged as idempotent no-ops. This cleanup never removes
the exact case cache or executable cache, so it does not reduce cache hit rate and
does not require a periodic full-store retention scan.

`run_id` identifies one immutable judgehost task. Repeating the same request is
idempotent; reusing the same `run_id` with a different payload is rejected.

## Cache Behavior

Current cache behavior for execution results:
- only exact case cache remains
- solve-output cache has been removed
- cache lookup happens lazily during `fetch-work`, before work is sent to judgedaemon
- same-key cache reads/materialization are serialized without blocking unrelated keys
- cache-hit results still update `verification_tasks` and artifact refs through the normal batched finalize path
- testcase input/answer registration uses metadata-only hits and does not rewrite an existing valid blob

## Worker Queue

`WorkerQueueService` is the async execution backbone.

Current facts:
- verification, run, export, and contest jobs run through the worker queue
- preview compile does not; it stays synchronous in the request path
- worker queue writes a JSONL durable log under `cache_root/runtime/worker-queue-events.jsonl`
- startup clears that log and resets inflight jobs

## Current Filesystem Touch Points During Verification

A single verification can write to these places:
- `artifacts/verifications/<verification_id>/tests/`, `ans/`, `logs/`, `bin/`, `uploaded-sources/`
- `runtime/snapshots/<snapshot_id>/src`
- `runtime/judgehost-runs/<judgehost_task_id>/...`
- `runtime/judgehost-executables/<kind>/<script_hash>/...`
- `judge-fs-index/...`
- `runtime/worker-queue-events.jsonl`

By current runtime policy, cache-root data is startup-cleared. Judgehost executable cache is not cleared at verification completion.
