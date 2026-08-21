# `app/service/execution`

Owns the canonical execution evidence shared by judgehost, verification, and
custom runs. `model.py` defines immutable result, outcome, compile, pass,
resource-usage, warning, and cache-ref values. `policy.py` validates and
normalizes evidence, including contiguous passes, capture completeness, and
aggregate usage. `codec.py` is the sole strict JSON boundary for persisted
execution results. `identity.py` owns execution `run_id` creation and
validation. `test_rows.py` maps canonical execution evidence to the shared
test/pass read shape without depending on verification storage.

The package depends only on canonical execution values. Verification identity,
task storage, judgehost transport, HTTP, and runtime blob availability belong to
their owning services. Cache refs are evidence locators; storage services own
their authorization and current availability.

See the [execution protocol](../../../../protocol/execution.md) for lifecycle,
result, and availability semantics.
