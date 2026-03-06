# PROGRESS

Last updated: 2026-03-06

This file is a compact, code-verified status snapshot.

## Current Baseline

1. Owner-scoped model and routes are in production use (`<owner>/<slug>`).
2. Authoring workflow is end-to-end (statement/files/generators/checker/validator/interactor/tests/solutions/verifications/packages).
3. Async worker queue is used for run/verification/export/contest jobs.
4. Invocation backend abstraction is live (`auto`, `local-sandbox`, `domjudge-judgehost`).
5. Judgehost integration is operational with `/api/v4/*` API surface.
6. Security baseline is active (sandbox fail-closed startup, sudo-protected destructive actions).
7. Build cache key uses schema `v3` with generation/toolchain digests; same-key build join behavior is active.
8. Statement/verification stale checks use quick-fp signatures (`size + mtime_ns`) instead of file-content streaming hash.

## Known Major Risks

1. Sandbox hardening depth (mount/seccomp/cgroup) is incomplete.
2. Cross-job cancellation/restart semantics are not fully unified.
3. Judgehost performance on very large test sets still needs tuning.

## Active Backlog

Active implementation backlog is maintained in `BACKEND_TODO.md`.
