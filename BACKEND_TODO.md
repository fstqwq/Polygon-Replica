# BACKEND_TODO

Last updated: 2026-03-06

Only active backend debt is listed here.

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
- Problem: local and judgehost paths still diverge in error payload shape.
- Exit criteria: one schema used by run details, verification, and export pages.
- Status: open.

2. Cache observability and diagnostics
- Problem: misses and evictions are not attributable enough for ops.
- Exit criteria: metrics for join wait, integrity miss, eviction reason, and validation failure.
- Status: open.

3. ICPC export conformance regression
- Problem: CI conformance checks are not strong enough.
- Exit criteria: deterministic CI gate with pass/fail report against target format.
- Status: open.
