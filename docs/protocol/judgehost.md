# Judgehost wire protocol

## Trust and authentication

Judgehost is an operator-controlled trusted deployment. Every `/api/v4/*`
request passes Judgehost authentication before work, files, or result state is
exposed. Authenticated compile, executable-cache, runtime, and result reports are
accepted as execution facts.

The API accepts a bearer token in `Authorization`, the same token in
`X-Judgehost-Token`, or HTTP Basic authentication using the configured username
and token as its password. A disabled API returns 404, an enabled API without a
configured token returns 503, and invalid credentials return 401.

The hostname supplied by registration, fetch-work, version reporting, or a
host-scoped callback is the scheduling and lease identity. It MUST contain 1-128
ASCII letters, digits, dots, underscores, or hyphens. Invalid hostnames receive
HTTP 400 and MUST NOT be replaced with a shared fallback identity.

## Endpoints

The implemented surface is:

| Method | Path |
| --- | --- |
| `GET` | `/api/v4/config` |
| `GET` | `/api/v4/languages` |
| `GET`, `POST` | `/api/v4/judgehosts` |
| `POST` | `/api/v4/judgehosts/fetch-work` |
| `GET` | `/api/v4/judgehosts/get_files/source/{item_id}` |
| `GET` | `/api/v4/judgehosts/get_files/source/{contest_id}/{item_id}` |
| `GET` | `/api/v4/judgehosts/get_files/{file_type}/{item_id}` |
| `GET` | `/api/v4/judgehosts/get_version_commands/{judgetask_id}` |
| `PUT` | `/api/v4/judgehosts/check_versions/{judgetask_id}` |
| `PUT` | `/api/v4/judgehosts/update-judging/{hostname}/{judgetask_id}` |
| `POST` | `/api/v4/judgehosts/add-judging-run/{hostname}/{judgetask_id}` |
| `POST` | `/api/v4/judgehosts/add-debug-info/{hostname}/{judgetask_id}` |
| `POST` | `/api/v4/judgehosts/internal-error` |

The route definitions and transport parsing are owned by `app/route/judgehost_route.py`
and `app/impl/judgehost/api.py`. Internal scheduling is not part of the external
wire format. DOMjudge's array-valued `add-debug-info` payload is
`multipart/form-data`; the endpoint accepts that official encoding.
DOMjudge also sends result files as base64-encoded, non-file multipart fields.
The server passes the configured encoded-field limit to each request parser;
it does not rely on a process-global parser default. A malformed or oversized
callback receives non-2xx and is never converted into an empty result. When the
path identifies an active case, the server first persists a bounded internal
failure describing why the callback was rejected, so Verification details do
not replace a transport failure with a guessed checker or validator message.

## Leasing and evidence

Registration releases leases previously owned by that hostname and returns the
DOMjudge job/submission pairs whose local workdirs may be removed. Fetch-work
selects a ready batch and leases cases to the authenticated hostname. A final
callback for an actively leased or reporting case from a different hostname is
invalid and receives non-2xx. A case result is claimed while it is processed so
concurrent final callbacks do not both publish state.

DOMjudge 9.0.1 has two different final-upload paths. Its asynchronous uploader
for a correct case treats any successful HTTP response as delivery and does not
interpret the body. Its synchronous incorrect-result path converts the response
body to a boolean to decide whether to continue with remaining work. The
official server returns its `hasFinalResult` decision from this endpoint; that
value is neither a completion receipt nor a task identity. The relevant
upstream implementation is documented by the pinned
[daemon](https://github.com/DOMjudge/domjudge/blob/90bbb727906efb438ac2ec7512c09f17824cfc41/judge/judgedaemon.main.php)
and
[server controller](https://github.com/DOMjudge/domjudge/blob/90bbb727906efb438ac2ec7512c09f17824cfc41/webapp/src/Controller/API/JudgehostController.php).
The Docker mock asserts Polygon-Replica's declared API contract directly; it
does not clone or approve upstream source before running.

Polygon-Replica deliberately defines a narrower project ACK: successful and
idempotent `add-judging-run` responses are the JSON integer `1`. A newly accepted
result bound to a verification task receives `1` only after the canonical
`ExecutionResult` and verification completion transaction are durable.
Persistence failure returns non-2xx so the daemon retries. The in-memory case is
then marked `completion_acknowledged`. `all`, `sample`, `custom`, and internal
`package` Verifications follow this same durable callback boundary.
A retry whose durable task is already terminal, or whose case is cancelled or
retired, also receives `1`. Invalid hostnames and active
lease-owner mismatches receive non-2xx; invalid hostnames are HTTP 400 and a
concurrent claim is HTTP 503.

A verification case is staged as non-fetchable, bound to its durable task and
active coordinator, and only then exposed to fetch-work. Its callback therefore
carries that task identity directly and does not depend on installing a reverse
`(judgetask_id, test_name)` mapping after work is visible. The owning result and
transaction semantics are in the
[execution protocol](execution.md#results-and-cache-payloads).

Before task admission, submission sources, auxiliary compile sources, inputs,
and answers are fixed in the runtime blob store. Batch state therefore owns
content-addressed runtime descriptors rather than paths into a temporary Git
snapshot. Removing a completed or failed Verification snapshot cannot break a
later materialization of an already admitted task.

After a batch is leased, dispatch reports its durable verification task through
an injected `CaseLeaseSink`. Judgehost does not import or locate the
verification coordinator. A missing process-local runtime is tolerated because
lease state is an overlay and the durable task decision remains authoritative.

Each leased case also receives a process-local monotonic deadline. Its budget
contains any still-required compilation, the case's configured execution and
comparison limits, and a final callback/transport grace period. Multi-pass
solution cases multiply the run and comparison allowance by the pass limit.
When one fetch returns several cases, each later deadline includes the budgets
of the cases before it. A successful final report rebases the remaining cases
from that report time; heartbeat, fetch traffic, and failed callback retries do
not extend them.

Maintenance checks case deadlines before host liveness. A leased case whose
deadline has elapsed and which has no active callback receipt becomes a
canonical infrastructure-failure (`FL`) result, allowing its task and
Verification to reach a terminal state even while the daemon remains online.
A reporting case is not expired. The old case identifier is never leased again;
a later final callback receives the ordinary idempotent `1` ACK and cannot
replace the terminal result.

The internal verification-to-Judgehost boundary names the task's program
identity `verification_program_id`; verification code uses the shorter
`program_id`. The process-local batch runtime keys a program batch by
`(verification_id, verification_program_id)`. Cases for additional tests join
that batch only while its source, compile specification, and execution identity
remain unchanged, so one verification program is compiled once and run against
its tests. A per-task `run_id` remains execution evidence and never identifies
this program.

A terminal compile or internal program failure remains attached to the open
program batch while dependency-gated tasks are still becoming runnable. A later
task that joins that batch receives the stored canonical failure immediately;
it is published through the ordinary durable completion path and is never made
fetchable to a host.

`compile_key` has a different purpose: it is the content-addressed identity of a
compilation cache entry. Different verification programs can produce the same
`compile_key` and reuse that entry without becoming the same program. Neither
`verification_program_id` nor `compile_key` is added to the external `/api/v4/*`
wire shape merely to expose internal scheduling.

## Compile, internal-error, and late diagnostics

A failed compile is terminal through `update-judging`; the DOMjudge daemon does
not subsequently send `add-judging-run` for that task. A first final run report
or an `internal-error` received while a case is active may likewise create its
canonical decision. The batch runtime claims these competing candidates under one
lock. Once a final report owns a case's reporting phase, a later program failure
cannot replace it; the SQLite completion transaction then supplies the durable
first-wins boundary.

After that decision, `add-debug-info` and `internal-error` are project late-
diagnostic inputs. They append bounded, retry-deduplicated diagnostic items and
never reopen the task, change its verdict or locators, release a successor, or
replace the verification's failure reason. Diagnostics received after decision
capture but before durable completion remain pending on the case; successful
completion flushes them, and failed persistence leaves them pending for retry.
Only a persisted item or recognized duplicate clears pending state. A sink
exception or `not-applicable` response for a bound verification task leaves the
item pending and makes the driving callback non-2xx. A case with pending
diagnostics is not eligible for quiet cleanup.

This diagnostic interpretation is not the official DOMjudge server model. In
DOMjudge 9.0.1, fetched `debug_info` work uploads either a `full_debug` package or
an `output_run`; the official controller stores that package or replaces the
judging-run output. Its `internal-error` endpoint creates or groups a separate
`InternalError` record and can disable the reported object. Polygon-Replica does
not claim those storage and disablement semantics.

## Interactive and multi-pass evidence

For interactive or multi-pass work, historical evidence is carried in the
existing `team_message` callback field as an uncompressed tar bundle. A bundle
has an empty `.polygon-pass-bundle` marker, a positive canonical
`final-pass-number`, and contiguous `passes/{number}/...` members. Historical
passes contain either all of `input`, `program.out`, `program.err`, `system.out`,
`program.meta`, `compare.meta`, `judgemessage.txt`, and `teammessage.txt`, or the
reduced set `{input, program.meta, compare.meta}`, or `{program.meta,
compare.meta}`. The final pass contributes `input`, `judgemessage.txt`, and
`teammessage.txt` alongside the callback's ordinary final-pass fields.

A valid bundle becomes per-pass evidence in the structured execution result.
Missing, reduced, or invalid historical capture is retained as a result warning
rather than reconstructed by the server. The accepted case report carries the
final verification evidence.

Multi-pass capture reads DOMjudge's pass directories directly. Pass 1 input is
kept by the testcase-local `.polygon-pass-1-input` hard link before DOMjudge
replaces `1/testdata.in` with its cache symlink. For pass-fail work, the run
wrapper locks history before the contestant starts and exposes traversal plus a
fixed read-only file set only after it exits. Interactive jury code runs as the
trusted `domjudge` user and leaves historical pass directories at mode `0700`.

A terminal callback carries one tar assembled directly from those files. It is
either a complete historical capture or metadata-only history with the byte
offset needed to separate final cumulative feedback. Capture does not copy a
historical tree, hash content, or alter contestant/checker exit status. A pass
that exceeds DOMjudge's native pass limit ends through `internal-error`.

DOMjudge does not create `teammessage.txt` when a checker or interactor has no
team-facing message. Capture represents that optional message as a zero-byte
regular member so its absence cannot discard the remaining pass evidence.

## Files, cache, and versions

Source and testcase files are served by opaque DOMjudge-compatible identifiers;
typed executable downloads accept `compile`, `run`, or `compare`.
Judgehost executable entries live in the per-key JudgeFS cache. They are runtime
scoped and startup-cleared, not verification-scoped.

The Judgehost language catalog advertises C++, Java, and Python. C is not a
separate submission language: `.c` sources are rejected at the canonical
problem-source boundary and are not assigned a Judgehost compile specification.

Compiler and runner version reports are accepted only for the current lease
owner and stored as process-local, per-host/language telemetry. Missing,
malformed, inactive-task, and non-owner reports are ignored. The server neither
rejects version differences nor includes reported versions in compile-cache
identity.

## Callback admission, restart, and cancellation

Startup cancels in-flight Judgehost work and clears runtime JudgeFS/blob state. Explicit cancellation first commits the Verification and all open tasks in one SQLite transaction, then closes Verification-level Judgehost admission and enqueues a process-local drain. The HTTP request does not walk its cases or publish a second cancellation for each task.

The drain processes bounded slices and releases the batch-state lock between them. Pending, staged, cache-probing, and unclaimed leased cases become acknowledged runtime cancellations directly. A reporting case or a case with an immutable callback receipt receives `cancel_requested`; the callback releases that receipt while converging the case to the same terminal state. Registry tasks are retired through the Verification index, and cancelled batches use a runtime-only finalization path. A failed slice is retried while admission remains closed.

An already leased or reporting case may remain in the current process for callback and workdir cleanup, but its ordinary result cannot amend the terminal decision or populate the result cache after cancellation wins. Late and repeated final callbacks receive `1`; they do not invoke strict durable completion or trigger Judgehost retries. Runtime tombstones remain until the ordinary quiet cleanup window, and startup recovery may discard them because SQLite already holds the authoritative cancellation.

Every state-writing `/api/v4` callback first enters a service-level admission
gate. Administrative cleanup closes that gate and proceeds only when the active
callback count is zero. If cleanup closes it first, later callbacks receive the
protocol-compatible idempotent response without accessing SQLite, runtime blobs,
telemetry, or the batch runtime: registration returns `[]`, version/update/debug
returns `{}`, `add-judging-run` returns `1`, and `internal-error` returns `0`.
If a callback enters first, cleanup reports busy with the
`judgehost_callbacks` reason.

Within the batch runtime, a callback acquires an immutable case receipt before it
releases the runtime lock. Quiet cleanup removes a retired case only after all
work is terminal, all callback receipts are released, pending diagnostics are
empty, and the verification has been quiet for 60 seconds. Thus callback-first
waits for completion, while cleanup-first makes a later callback an ACK/no-op;
no callback holds the runtime lock while writing SQLite.

Startup has no receipt window to recover. It atomically terminalizes unfinished
verification and task rows before deleting runtime blobs and Judgehost state;
failure of that durable step aborts startup. Callbacks for state absent after a
restart are acknowledged idempotently so the daemon does not retry forever and
do not create persistent receipt records.
