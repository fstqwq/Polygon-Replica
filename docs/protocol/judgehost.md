# Judgehost wire protocol

## Trust and authentication

Judgehost is an operator-controlled trusted deployment. Every `/api/v4/*`
request passes Judgehost authentication before work, files, or result state is
exposed. Authenticated compile, executable-cache, runtime, and result reports are
accepted as execution facts.

The hostname in a host-scoped route is the scheduling and lease identity. It
MUST be non-empty and match the server's hostname grammar. Invalid hostnames
receive HTTP 400 and MUST NOT be replaced with a shared fallback identity.

## Endpoints

The implemented surface is:

- `GET /api/v4/config` and `GET /api/v4/languages`
- `GET|POST /api/v4/judgehosts`
- `POST /api/v4/judgehosts/fetch-work`
- source and typed file downloads below `/api/v4/judgehosts/get_files/...`
- version commands and version reports for a judging task
- `PUT .../update-judging/{hostname}/{judgetask_id}`
- `POST .../add-judging-run/{hostname}/{judgetask_id}`
- debug-info and internal-error callbacks

The route definitions and transport parsing are owned by `app/route/judgehost_route.py`
and `app/impl/judgehost/api.py`. Internal scheduling is not part of the external
wire format.

## Leasing and evidence

Fetch-work selects a ready batch and leases cases to the authenticated hostname.
A final callback from a different hostname is invalid and receives non-2xx. A
case result is claimed while it is processed so concurrent final callbacks do
not both publish state.

`add-judging-run` returns the JSON integer `1` when a final result is accepted.
It also returns `1` for an idempotent retry whose case is already terminal,
cancelled, or absent after runtime cleanup. Malformed payloads, invalid
hostnames, and lease-owner mismatches return non-2xx. The numeric task id is not
a callback receipt.

Interactive and multi-pass reports retain per-pass evidence in the serialized
execution result. The final verification view is derived from the accepted
Judgehost case report rather than from a separate evidence protocol.

## Files, cache, and versions

Source and input files are served by opaque DOMjudge-compatible identifiers.
Judgehost executable entries live in the per-key JudgeFS cache. They are runtime
scoped and startup-cleared, not verification-scoped.

Compiler and runner version reports are associated with the reporting host and
leased task and stored as process telemetry. The server currently neither
rejects version differences nor includes reported versions in compile-cache
identity.

## Restart and cancellation

Startup cancels in-flight Judgehost work and clears runtime JudgeFS/blob state.
Late callbacks for work removed by that cleanup are acknowledged idempotently so
the daemon does not retry forever. User cancellation waits for leased-case
receipts when possible and finalizes according to the current batch scheduler.
