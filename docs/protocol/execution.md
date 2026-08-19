# Execution and verification protocol

## Job model

Preview compilation is synchronous in the HTTP request. Contest HTML Review
uses a bounded worker pool but the POST remains blocked until every Problem has
reached a terminal result; it does not poll fragments or embed iframes. Contest
PDF Preview runs the full, single-document Contest TeX pipeline in that request
and writes only to Preview cache. Contest package downloads likewise build the
selected bundle synchronously in the request. Verification, custom run, and
export jobs are submitted to one bounded process-local worker queue. Queue records and `worker-queue-events.jsonl` are runtime
diagnostics; they are cleared at startup and do not recover work after restart.
Durable job summary rows are reconciled to failed states during startup.

A custom run is represented as a verification with the custom kind. It uses the
same task storage, Judgehost dispatch, results, and cache-payload model.

## Verification lifecycle and DAG

Verification kinds are `all`, `sample`, and `custom`. Their reachable durable
state transitions are:

```text
absent -> queued -> running -> ok | failed | cancelled
          |  \---------------> failed | cancelled
```

Admission inserts a `queued` verification with its request identity and no task
rows. Planning uses a frozen workspace snapshot. Activation changes that row
from `queued` to `running` and writes the complete detail and complete task graph
in one transaction. A plan is installed once: it is never deleted, replaced, or
partially extended. Explicit user cancellation changes an active verification
to `cancelled`; planning, queue, infrastructure, and startup interruption
change it to `failed`. Reason text never determines the lifecycle state.

`pending` is a task-derived display state, not a persisted verification
lifecycle state. An `ok` verification has a complete graph, terminal tasks, and
completed sanity processing. A sanity warning or failure remains attention
detail on an `ok` verification; it does not reopen or fail the task decision.
A `failed` verification may have no graph when it failed before activation. A
`cancelled` verification records an explicit user action. Either terminal
transition atomically cancels remaining open tasks without rewriting completed
task evidence. A terminal verification cannot return to an active state.
Startup fails all verification work left in `queued` or `running`;
coordinators and leases are not reconstructed.

A verification plan has two related identities:

```text
VerificationProgram
  - program_id
  - kind
  - source_path
  - compile_spec

VerificationTask
  - program_id
  - test_name
```

A `VerificationProgram` means one source program under one normalized compile
specification. The generator is a program, the accepted solution is a program,
and every checked solution is a separate program. Multiple test tasks may
reference the same program and therefore share its one Judgehost compilation.
Generator parameters remain part of each task's input payload: they change the
invocation and result-cache identity, but not the generator program identity.
If compilation or an active internal error has already failed that program,
test tasks that become runnable later inherit the same canonical program
failure and finish without waiting for another compile.

The accepted program uses `accepted`. Distinct logical generator definitions
receive `generator-<first-seen-index>`, and checked solutions receive
`solution-<plan-index>` in the verification's deterministic target order.
Generator paths remain distinct programs even
when their current content and compile specification are equal. These names
identify role and deterministic position in this verification plan; they are
not global source or cache identities.

A task also carries its task kind, source, expected behavior, optional
predecessor, final status, and serialized execution result. An empty
`final_status` is the only durable open state; `done`, `failed`, and `cancelled`
are terminal. Pending, queued, and leased are process-local task overlays used
by read models; reporting is a Judgehost case phase rather than a task row
state.

The durable task ID is the path-safe natural key
`vt~<verification_id>~<program_id>~<test_name>`. Activation recomputes every
task key and requires all tasks in one program to retain one consistent program
definition. It rejects inconsistent or duplicate plan members instead of
allocating another identity.

Within verification code this identity is `program_id`. At the boundary into
Judgehost scheduling it is named `verification_program_id`, so it cannot be
confused with the per-execution `run_id`. Judgehost uses
`(verification_id, verification_program_id)` to collect the test cases that
share a compilation. `compile_key` is a separate content-addressed compile-cache
identity; two different programs may have the same `compile_key` and reuse the
same cached compilation.

The graph has exactly three task kinds:

- `generate-input` materializes one testcase input. Manual tests use a trivial
  program over the authored input payload; generated tests execute the selected
  generator.
- `main-correct` runs the accepted solution after that test's input is ready.
- `solution-run` runs another solution after the same test's `main-correct`
  task succeeds.

Thus accepted-solution execution gates the other expected-behavior checks per
test. Identical generator invocations share one generated result; duplicate
generation tasks depend on the owning invocation and finish as skipped.

Generator-backed tests execute their generator payload, including parameters.
The configured validator runs as the generator task's checker between
generation and solution execution.

The DAG scheduler publishes runnable tasks in bounded batches and polls
Judgehost case-cache misses. Judgehost terminal reports, cache hits, and
terminal reconciliation all pass through the same completion service. The
coordinator receives the effective persisted completions and advances
dependants from that state; it does not receive an uncommitted Judgehost result.
Predecessor failure or cancellation skips or cancels dependants. Compile-only,
pass-fail, interactive, and multi-pass execution are mapped to Judgehost batches
and case results by the verification and Judgehost services.

One process-owned `VerificationRuntimeRegistry` maps an active verification to
its coordinator. Registration is insert-only and unregistration must present
the same coordinator object, so a stale worker cannot remove another session.
The registry lock protects only the object map; it is released before an event
is enqueued. Completion notification and Judgehost lease notification use this
injected registry rather than module-global scheduler functions. A missing
runtime notification is a no-op because the task transaction remains durable.
If direct completion-event delivery fails while a runtime is registered, the
registry requests a durable task-snapshot reconciliation. The coordinator also
performs that reconciliation after an idle interval before advancing ready
successors. If both event paths fail, the caller receives an error preserving
both causes. When the idle check instead discovers that the durable parent is
already terminal, the coordinator drains Judgehost execution before retiring.
Lease notification performs one current-owner retry; if both attempts fail,
the fetch request fails so the process-local lease can expire and be requested
again instead of silently losing coordinator state.

`VerificationWorkflow` owns workspace snapshot acquisition and cleanup,
planning, task publication, and sanity callbacks. Callers may instead supply an
already frozen snapshot for the published-revision and package workflows.
`VerificationExecutionService` owns coordinator construction, registration,
durable-state reconciliation, execution, exact unregistration, scheduler
failure, and user cancellation. Neither HTTP code nor the workspace service
owns the runtime session.
The coordinator is constructed from the immutable durable graph, then
registered before execution starts. The execution service rereads the parent
snapshot after registration. If it is already closed, the coordinator consumes
a closed event and reloads terminal task state instead of publishing work. A
cancellation that committed before registration is therefore observed from
SQLite, while a later cancellation reaches the registered coordinator.

Completion evaluates each `solution-run` from its canonical `ExecutionResult`,
not the batch transport status or summary. At the testcase boundary, AC, WA, TL,
RE, and CE are complete decisions and only the expected behavior's `allowed`
set applies. An FL, missing, incomplete, or disallowed decision fails that task
and stores the first failure reason. The `required` set belongs to the program:
after every durable task for one `program_id` is terminal without a case-level
failure, the completion transaction aggregates its testcase verdicts and checks
`required` once. These testcase rules are distinct from package-level final
result sets: `tle_or_re`, for example, allows AC, TL, and RE testcases but
requires at least one TL or RE across the complete program.
Skipped duplicate-input tasks contribute no verdict. A missing required verdict
does not rewrite the completed testcase tasks, but it stores the verification's
first failure reason. Independent solution tasks continue, and once no task
remains open that stored mismatch makes the parent `failed`. Thus an expected CE
is a successful task and can satisfy a program requirement even when Judgehost
reports the batch as failed. Generator, `main-correct`, and unexpected task
cancellation failures are hard failures: they fail the parent and cancel all
remaining open tasks immediately in the same transaction.

Explicit user cancellation atomically changes the parent to `cancelled` and
every open task to `cancelled`. Already leased or reporting cases then drain in process-local
Judgehost state, but their late ordinary results cannot change the durable
decision. User cancellation and scheduler failure always order their side
effects as the SQLite parent/task transition, then the coordinator event, then
Judgehost drain. Scheduler failure uses `failed`; user cancellation uses
`cancelled`. Drain is attempted even when the in-memory cancellation notification
fails; a failed cancel notification falls back to a closed event, and idle
coordinators reconcile task rows and compare the durable parent state. Drain
has one immediate
retry, and a later cancel/fail request retries it even when the parent is already
closed. A synchronous hard failure stops the current publish slice before
another independent ready task can be exposed. A runtime
verification moves through dormant, active, draining, and retired phases
independently of the parent status. Draining state is retained for the current
process until all work and callback receipts are terminal and the verification
has been quiet for 60 seconds.

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

## Results and cache payloads

For a final `add-judging-run` callback, Judgehost first captures cache payloads
and refs, then a dependency-light normalizer produces the canonical case
`ExecutionResult` owned by `app.service.execution`. Compile failure arrives
through `update-judging`, and a
missing case has no complete final callback. Pure task-result projection builds
those failure results from stored compile/case evidence; finalization publishes
and aggregates terminal case results into the task report. Verification preserves
that report instead of rebuilding compile data, passes, warnings, or payload
evidence from summary fields.

Task results are serialized in `verification_tasks.result_json` only through
the strict execution codec. The canonical shape has `outcome`, `compile`,
ordered `passes`, and `warnings`; missing, additional, or incorrectly typed
fields are invalid. Each pass
records its number, capture status, run result, verdict, score, answer flag,
resource usage, feedback, and cache locators. Pass numbers are contiguous
from one; an output locator and transcript locator are mutually exclusive.

Verification converts a terminal report into one `TaskCompletion`. Generator
success requires an available, untruncated output blob and carries that locator
as `input_ref`; `main-correct` success requires an available output blob and
carries it as `answer_ref`. Task terminal state, result, the applicable locator,
and the verification's first failure reason are committed together as described
by the [SQLite persistence contract](persistence.md#execution-rows).

Only a task without a terminal status accepts its first completion. A repeated
or conflicting completion yields the already-persisted result and locators and
does not amend them. Generator content deduplication and the resulting skipped
dependants are part of the same commit. The process-local coordinator consumes
the returned lifecycle commit; SQLite remains authoritative when no
coordinator exists or a notification is repeated.

Compile failure reported through `update-judging`, the first final run report,
and an internal error received while a case is active can create the canonical
terminal result. Their first decision claim is serialized under the Judgehost
batch-runtime lock; a program failure can finish unclaimed cases but cannot replace
a final result whose case is already reporting. SQLite first-wins completion is
the second durable guard. Evidence received after that decision does not amend it.
Terminal `add-debug-info` and `internal-error` callbacks append normalized
`debug-info` or `internal-error` items to the task's late-diagnostic snapshot.
Each item records hostname, bounded text, receipt time, and a content digest for
retry deduplication. The digest covers kind, hostname, and bounded text.
Snapshot size is bounded by the auxiliary display limit; oldest items are
removed first when necessary.

Late diagnostics do not change task or verification status, verdict, canonical
result, input/answer locators, first failure reason, or DAG readiness. Detail
reads compose them with the immutable result for display. An ordinary duplicate
compile or final-result callback is an idempotent retry, not diagnostic
evidence.

The canonical serialized result carries pass evidence. Every non-empty pass ref,
plus per-test generated input and accepted answer refs, is indexed in the
currently named `verification_task_artifacts` table by owning task, test, pass,
and role. These refs all identify cache payloads. Downloads use the ownership
index for authorization and locator lookup.

Locators point into cache storage. A durable terminal result may remain after
startup while one or more payloads are unavailable. Downloads MUST resolve the
locator through the owning store and fail as unavailable when the cache entry is
missing. Verification detail downloads use
`/problems/{problem:path}/artifacts/{verification_id}/{rel_path:path}`.

Interactive transcript detail reads retain the first 100 events and scan at most
1000 protocol events. A transcript that ends within that scan window reports its
exact event total. If more data remains after the 1000th event, parsing stops and
the UI reports only that it is showing the first 100 events; it does not claim an
exact total. Statement sample and sample JSON projection use the same bounded
parser and reject a scan-limited transcript instead of deriving partial authored
sample content.
