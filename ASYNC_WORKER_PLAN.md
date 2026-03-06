# ASYNC_WORKER_PLAN

Last updated: 2026-03-07

## Goal

Provide one reliable async substrate for:

- run execution
- verification
- export/package jobs
- judgehost-backed execution paths

## Invariants

- On service restart, all pre-restart unfinished jobs are terminalized.
- No pre-restart job is resumed.
- Terminalization mapping is deterministic:
  - queued -> `cancelled` (`worker_cancelled`)
  - running -> `cancelled` (`worker_cancelled`)

## Open Async Gaps

1. Cancellation semantics are still fragmented across job types.
2. Failure payload schema is not fully unified (`error_code/message/stderr_excerpt`).
3. Payload and memory governance for large workloads needs tightening.
4. Cross-process long-window observability is limited.

## Acceptance

1. Async APIs remain responsive under load and return stable job ids.
2. Restart terminalization is immediate and visible in UI/API.
3. No restart-failed job transitions back to `running`.
4. Operators can answer: queued/running now, restart-failed jobs, failure cause, degraded job type.
