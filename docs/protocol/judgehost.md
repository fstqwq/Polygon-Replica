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
wire format.

## Leasing and evidence

Registration releases leases previously owned by that hostname and returns the
DOMjudge job/submission pairs whose local workdirs may be removed. Fetch-work
selects a ready batch and leases cases to the authenticated hostname. A final
callback for an actively leased or reporting case from a different hostname is
invalid and receives non-2xx. A case result is claimed while it is processed so
concurrent final callbacks do not both publish state.

`add-judging-run` returns the JSON integer `1` when a final result is accepted.
It also returns `1` for an idempotent retry whose case is already terminal,
cancelled, or absent after runtime cleanup. Invalid hostnames and active
lease-owner mismatches return HTTP 400; a concurrent claim returns 503. The
numeric task id is not a callback receipt.

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
rather than reconstructed by the server. The final verification view is derived
from the accepted case report; there is no separate evidence protocol.

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
that exceeds DOMjudge's native pass limit ends through `internal-error`; there
is no final artifact callback for that pass.

DOMjudge does not create `teammessage.txt` when a checker or interactor has no
team-facing message. Capture represents that optional message as a zero-byte
regular member so its absence cannot discard the remaining pass evidence.

## Files, cache, and versions

Source and testcase files are served by opaque DOMjudge-compatible identifiers;
typed executable downloads accept `compile`, `run`, or `compare`.
Judgehost executable entries live in the per-key JudgeFS cache. They are runtime
scoped and startup-cleared, not verification-scoped.

Compiler and runner version reports are accepted only for the current lease
owner and stored as process-local, per-host/language telemetry. Missing,
malformed, inactive-task, and non-owner reports are ignored. The server neither
rejects version differences nor includes reported versions in compile-cache
identity.

## Restart and cancellation

Startup cancels in-flight Judgehost work and clears runtime JudgeFS/blob state.
Late callbacks for work removed by that cleanup are acknowledged idempotently so
the daemon does not retry forever. User cancellation waits for leased-case
receipts when possible and finalizes according to the current batch scheduler.
