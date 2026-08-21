# `app/service/execution`

Owns canonical execution results shared by judgehost, verification, and custom runs. It defines immutable outcome, compile, pass, resource, warning, and cache-reference values; validates complete ordered evidence; provides the strict persistence codec; and derives shared test/pass read models.

Verification identity, task storage, judgehost transport, HTTP, and runtime blob availability remain with their owning services. Cache references are evidence locators, not storage authority.

See the [execution protocol](../../../../protocol/execution.md) for lifecycle,
result, and availability semantics.
