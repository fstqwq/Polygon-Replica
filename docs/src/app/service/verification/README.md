# `app/service/verification`

Owns verification and custom-run identities, source manifests and signatures,
execution plans, task persistence, DAG scheduling, result normalization,
expected-behavior matching, and read models. It consumes a frozen workspace
snapshot, selected solutions/tests, canonical runtime configuration, and typed
Judgehost terminal reports containing a canonical `ExecutionResult`. It
produces durable verification/task rows and runtime blob locators; it does not
own the Judgehost wire protocol or blob filesystem.

The completion service validates required generator and accepted-solution
outputs, creates a single `TaskCompletion`, and asks the task store to commit the
terminal result, dependent locator, and first failure state together. It accepts
Judgehost publication through a narrow completion sink and has explicit task
store and runtime blob store dependencies; it does not query global runtime
configuration. Cache hits, terminal reconciliation, cancellation, and the
no-coordinator fallback use the same completion boundary.

The process-local coordinator exists only while a verification is active;
it consumes `CompletionCommit` values after SQLite commit and is not a durable
fact source. SQLite rows survive restart and startup reconciliation terminalizes
interrupted work. Artifact bytes remain cleanup-safe. See the
[execution protocol](../../../../protocol/execution.md) for graph, identity,
result, and availability semantics.
