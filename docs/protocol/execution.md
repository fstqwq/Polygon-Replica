# Execution and verification protocol

## Work model

| Work | Execution |
| --- | --- |
| Problem statement HTML/PDF | Synchronous request with preview-cache reuse. |
| Contest statement HTML/PDF | Synchronous request; HTML may render problems through a bounded pool. |
| Contest package bundle | Synchronous request that submits missing external exports to the worker queue, waits for them, and assembles verified cache archives. |
| Verification, custom run, and package export | Bounded process-local worker queue with durable summary state. |

Process-local work does not resume after restart. Startup reconciles interrupted durable records to terminal failure before accepting new work.

## Verification lifecycle

| Kind | Scope |
| --- | --- |
| `all` | Full verification. |
| `sample` | Sample-only evidence, including statement preview preparation. |
| `custom` | User-selected program and tests. |
| `package` | Internal input generation and main-correct execution for a `not verified` native package. |

```text
absent -> queued -> running -> ok | failed | cancelled
```

Admission creates a taskless `queued` record for a frozen workspace snapshot. Activation atomically installs the complete task graph and changes the parent to `running`. A plan is installed once. User cancellation produces `cancelled`; planning, queue, infrastructure, and startup failures produce `failed`. Terminal status never reopens, and terminalization cancels every remaining open task without rewriting completed evidence.

A problem reader may start `all` verification or rejudge visible evidence into their own workspace. Custom Run remains problem-write-only because it accepts user-selected programs, tests, uploads, and cache-bypass controls. Every full-verification admission explicitly records in process whether the actor may publish matching evidence as native-package certification. Readers and `readonly` or `workspace` agent scopes cannot do so; a browser actor with package-create capability or a currently authorized `commit` agent scope can. Sample verification never certifies a package.

`pending` is a display projection, not a persisted parent state. An `ok` verification has terminal tasks and completed sanity processing. Sanity warnings remain attached to an otherwise successful verification. A pre-activation failure may have no task graph.

## Task graph

| Task | Behavior |
| --- | --- |
| `generate-input` | Use authored input or run the selected generator, then validate generated output when a validator is configured. |
| `main-correct` | Run the accepted solution after input is ready and retain its output as the official answer. |
| `solution-run` | Run another authored solution after the same test's main-correct task succeeds. |

Tasks for the same source program and compile specification share one judgehost compilation. Generator parameters belong to the invocation and result-cache identity, while program identity remains tied to source and compile configuration. Identical generator invocations share generated evidence; duplicate tasks remain ordered but do not execute twice.

Prepared payloads carry canonical `problem_mode`. Execution mode is derived from the task:

| Task | Execution mode | Components |
| --- | --- | --- |
| `compile-only` | `pass-fail` | Submitted source and explicit extra sources. |
| `generate-input` | `pass-fail` | Generator, validator, and required `testlib.h`. |
| `main-correct`, `solution-run` for pass-fail | `pass-fail` | Solution, checker, and required `testlib.h`. |
| `main-correct`, `solution-run` for interactive | `interactive` | Solution, interactor, and required `testlib.h`. |

A missing or invalid `problem_mode` rejects a non-compile task. The canonical authored memory limit is dispatched exactly in KiB. Full verification and package work use the background judgehost class; preview sample evidence uses foreground priority. Priority affects only work that has not started.

## Verdict and completion

AC, WA, TL, RE, and CE are complete testcase decisions. A solution task succeeds when its verdict belongs to the expected behavior's `allowed` set. After all tasks for one solution are terminal, its combined verdicts must also satisfy the solution-level `required` set. Skipped duplicate-input tasks contribute no verdict.

FL, missing or incomplete evidence, and disallowed verdicts fail the task. Generator, main-correct, infrastructure, and unexpected cancellation failures fail the verification immediately and cancel remaining work. A solution mismatch records the first failure reason while independent solution tasks continue; the parent becomes `failed` when no open task remains. Expected CE can therefore be a successful solution decision even when the underlying batch reports compile failure.

Each task accepts one durable completion. Repeated or conflicting callbacks receive the persisted result and cannot amend status, locators, or failure reason. Durable task decisions are committed before process-local coordination is notified; reconciliation reads SQLite when notification is lost.

The canonical task result contains `outcome`, compile evidence, ordered passes, and warnings. Each pass records its number, capture status, run result, verdict, score, answer flag, resource usage, feedback, and cache locators. Pass numbers start at one and are contiguous; output and transcript locators are mutually exclusive.

## Cancellation

Cancellation atomically marks the verification and every open task `cancelled`, closes judgehost admission for that verification, and returns before runtime cleanup finishes. A process-local drain retires its cases, batches, and registry entries without publishing per-case cancellation to SQLite.

A callback that loses the race to cancellation discards its result and cache candidate and receives the idempotent judgehost ACK. Leased or reporting cases may remain temporarily for callback and workdir cleanup, but they cannot change the durable decision. Repeating cancellation is safe. Startup may discard unfinished runtime drain state because SQLite already contains the authoritative cancellation.

## Identity and cache

Verification staleness is derived from `config/problem.json`, `config/build.json`, `tests/spec.json`, and regular files below `generators/`, `validators/`, `checkers/`, `interactors/`, `solutions/`, `tests/manual/`, `tests/generator/`, and `third_party/testlib/`. Missing roots and files participate in the signature. Statement-only changes do not stale verification.

Execution cache identity contains source, extra sources, input, generator parameters, and canonical run configuration. Compiler and runner versions are telemetry only. A cached result is reusable only for the same identity.

Case-result cache publication is first-writer-wins because equivalent executions may report different timing or captured bytes. The first valid result remains reusable. Executable-cache identity remains strict.

## Evidence and diagnostics

Generator success requires an available untruncated output payload and records it as input evidence. Main-correct success requires an available output payload and records it as answer evidence. Verification task results and their input, output, answer, feedback, transcript, and log locators are committed through the [SQLite persistence contract](persistence.md#execution-rows).

These payloads are cache. Durable summaries may outlive them; downloads resolve locators through the owning store and report unavailable payloads. Artifact ownership is indexed by verification, task, test, pass, and role for authorization.

Debug and internal-error reports received after a task decision become bounded, retry-deduplicated late diagnostics. They do not change task status, verdict, canonical result, locators, parent status, or dependency readiness.

Interactive transcript display retains the first 100 events and scans at most 1000. A transcript ending inside that window reports its exact total. If more data remains, display reports `Showing first 100 events.` Statement sample projection rejects scan-limited transcripts instead of deriving partial samples.
