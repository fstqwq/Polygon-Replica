# Judgehost wire protocol

## Trust and authentication

Judgehosts are operator-controlled execution workers. Every `/api/v4/*` request is authenticated before work, files, or result state is exposed. Authenticated compile, runtime, and result reports are accepted as execution facts.

The API accepts a bearer token in `Authorization`, the same token in `X-Judgehost-Token`, or HTTP Basic authentication with the configured username and token. A disabled API returns 404, missing server credentials return 503, and invalid credentials return 401.

The hostname supplied by registration, work fetch, version reporting, and host-scoped callbacks is the lease identity. It must contain 1-128 ASCII letters, digits, dots, underscores, or hyphens. Invalid hostnames return HTTP 400.

## Endpoints

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

`add-debug-info` and final result files use DOMjudge multipart encoding, including base64-encoded non-file fields. Encoded fields are bounded per request. Malformed or oversized callbacks return non-2xx; an active case also receives a bounded internal-failure diagnostic.

## Leasing and callback acknowledgement

Registration releases leases previously owned by the hostname and returns workdirs that may be removed. Fetch-work leases ready cases to that host. A final callback from another hostname or a concurrent callback claim returns non-2xx.

Successful and idempotent `add-judging-run` responses are the JSON integer `1`. A new result bound to a verification task receives `1` only after its canonical result and task completion are durable. Persistence failure returns non-2xx for retry. A retry for an already-terminal, cancelled, or retired case also receives `1`.

Sources, auxiliary sources, inputs, and answers are fixed in content-addressed runtime storage before a case becomes fetchable. Cases sharing one verification program and compile specification share compilation.

Each lease has a monotonic deadline covering remaining compilation, configured execution and comparison limits, pass count, earlier cases returned in the same fetch, and callback grace. A successful report rebases later cases from that fetch. Heartbeats, fetches, and failed callbacks do not extend deadlines.

An expired leased case without an active callback becomes an infrastructure-failure result. Reporting cases are not expired. Case IDs are never leased again after terminalization, and a late final callback receives the idempotent ACK without replacing the result.

## Results and diagnostics

| Input | Decision |
| --- | --- |
| Compile failure through `update-judging` | Terminal compile result. |
| First valid `add-judging-run` | Terminal run result. |
| `internal-error` while active | Terminal infrastructure result when it wins the first-decision race. |
| Debug or internal error after decision | Bounded late diagnostic. |

The first terminal decision wins. Late diagnostics are retry-deduplicated and never change status, verdict, result locators, dependency readiness, or the verification failure reason.

## Interactive and multi-pass evidence

Interactive and multi-pass callbacks carry historical evidence in `team_message` as an uncompressed tar archive. A valid archive contains:

- an empty `.polygon-pass-bundle` marker;
- a positive `final-pass-number`;
- contiguous `passes/{number}/...` members;
- historical pass metadata and any captured input, output, error, feedback, and team message;
- final-pass input and feedback alongside the callback's ordinary final-pass fields.

Valid members become ordered pass evidence. Missing, reduced, or invalid historical capture is retained as a warning while the final case report remains authoritative. A pass beyond DOMjudge's native pass limit reports `internal-error`.

## Files, languages, and versions

Source and testcase files use opaque DOMjudge-compatible identifiers. Executable downloads accept `compile`, `run`, or `compare` and live in the startup-cleared JudgeFS cache.

The language catalog advertises C++, Java, and Python. C source is rejected by the problem-source contract.

Compiler and runner versions are accepted only from the current lease owner and stored as process-local telemetry. Missing, malformed, inactive-task, and non-owner reports are ignored. Version differences neither reject work nor change compile-cache identity.

## Restart, cancellation, and maintenance

Startup fails unfinished durable execution before clearing runtime cases, leases, blobs, and JudgeFS state. Callbacks for state absent after restart receive idempotent responses.

Explicit verification cancellation first commits the parent and open tasks, then closes admission and drains runtime cases asynchronously. Later results cannot amend cancellation or populate result cache. Final callback retries receive `1`.

Administrative cleanup and backup close a service-level callback gate after active callbacks drain. Callbacks arriving after closure receive protocol-compatible no-op responses:

| Callback | Response |
| --- | --- |
| Registration | `[]` |
| Version, update, or debug | `{}` |
| `add-judging-run` | `1` |
| `internal-error` | `0` |

Runtime cleanup waits for terminal work, released callback receipts, and flushed diagnostics.
