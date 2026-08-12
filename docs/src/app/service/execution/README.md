# `app/service/execution`

Owns the canonical execution evidence shared by Judgehost, verification, and
custom runs. `model.py` defines immutable result, outcome, compile, pass,
resource-usage, warning, and artifact-ref values. `policy.py` validates and
normalizes evidence, including contiguous passes, capture completeness, and
aggregate usage. `codec.py` is the sole strict JSON boundary for persisted
execution results. `identity.py` owns execution `run_id` creation and
validation.

This package has no dependency on verification identity, task storage,
Judgehost transport, HTTP, or runtime blob availability. Artifact refs are
evidence locators; their ownership and current availability are handled by the
owning storage services.

See the [execution protocol](../../../../protocol/execution.md) for lifecycle,
result, and availability semantics.
