# BACKEND_TODO

Last updated: 2026-03-09

Only active backend debt is listed here.

Judgehost acceptance execution checklist (post-restart, Playwright-required, fixed four-problem pass) is maintained in `JUDGEHOST_ACCEPTANCE.md`.

## Latest Acceptance Anomalies (2026-03-09)

Playwright acceptance pass evidence:
- Matrix `inv-648517f30fa4`: FAILED with `compare script 223 crashed with exit code 3, expected one of 42/43`.
- Taxi `inv-381e8cd7c2c7`: FAILED with accepted-target mismatch `1_array.cpp: required=[AC], allowed=[AC], got=[WA]`.
- Fuzzy Ranking `inv-c4996938290e`: FAILED with `accepted solution failed on 001.in: combined run/compare script 242 crashed with exit code 3, expected one of 42/43`.
- Guess the Number `inv-e73dc48eeaeb`: OK.

Tracking note: this blocks green acceptance because expected outcomes require Matrix/Taxi/Guess to pass and Fuzzy to be pass or timeout.

## P0 (Must)

1. Sandbox hardening
- Problem: mount/seccomp/cgroup policies are not deep enough.
- Exit criteria: profile-specific policies (`compile`/`run`/`tex`) with regression tests.
- Status: open.

2. Queue state convergence
- Problem: state semantics still differ between async job types.
- Exit criteria: restart/cancel converge to terminal states with one shared status model.
- Status: open.

3. Judgehost scalability under large test sets
- Problem: per-case overhead and payload pressure still high at scale.
- Exit criteria: stable latency and bounded memory on high test-count problems.
- Status: open.

## P1 (Should)

1. Error model normalization
- Problem: judgehost-only code execution path still has inconsistent error payload details across run/verification/build surfaces.
- Exit criteria: one schema used by run details, verification, build solve, and export pages.
- Status: open.

2. Cache observability and diagnostics
- Problem: misses and evictions are not attributable enough for ops.
- Exit criteria: metrics for join wait, integrity miss, eviction reason, and validation failure.
- Status: open.

3. ICPC export conformance regression
- Problem: CI conformance checks are not strong enough.
- Exit criteria: deterministic CI gate with pass/fail report against target format.
- Status: open.
