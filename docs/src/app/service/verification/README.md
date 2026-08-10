# `app/service/verification`

Owns verification and custom-run identities, source manifests and signatures,
execution plans, task persistence, DAG scheduling, result normalization,
expected-behavior matching, and read models. It consumes a frozen workspace
snapshot, selected solutions/tests, canonical runtime configuration, and
Judgehost case events. It produces durable verification/task rows and runtime
blob locators; it does not own the Judgehost wire protocol or blob filesystem.

The process-local coordinator exists only while a verification is active;
SQLite rows survive restart and startup reconciliation terminalizes interrupted
work. Artifact bytes remain cleanup-safe. See the
[execution protocol](../../../../protocol/execution.md) for graph, identity,
result, and availability semantics.
