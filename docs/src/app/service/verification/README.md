# `app/service/verification`

Owns verification and custom-run identities, source manifests and signatures,
execution plans, task persistence, DAG scheduling, result normalization,
expected-behavior matching, and read models. It consumes a frozen workspace
snapshot, selected solutions/tests, canonical runtime configuration, and typed
Judgehost terminal reports containing a canonical `ExecutionResult`. It
produces durable verification/task rows and runtime blob locators; it does not
own the Judgehost wire protocol or blob filesystem.

The lifecycle service admits a verification once, atomically activates one
immutable execution plan, commits first-wins task decisions, finalizes sanity,
and terminalizes cancellation or restart interruption. The completion boundary
validates required generator and accepted-solution outputs and commits the task
result, dependent locator, parent transition, and remaining-task cancellation
together. It accepts Judgehost publication through narrow completion and
diagnostic sinks and has explicit task-store and runtime-blob dependencies; it
does not query global runtime configuration.

Planning models a program as `program_id`, kind, source path, and normalized
compile specification; a task selects that program and a test. The generator,
accepted solution, and every checked solution are programs, and all tasks under
one program share its compilation. The durable task identity is
`vt~<verification_id>~<program_id>~<test_name>`. At the Judgehost service
boundary `program_id` is passed as `verification_program_id`; the runtime
`run_id` and content-addressed `compile_key` remain separate identities.

Late diagnostics occupy a separate one-row-per-task snapshot. They augment
detail reads but cannot amend the canonical result, refs, verdict, failure owner,
or coordinator state. A verification snapshot reads parent, detail, graph,
locators, and diagnostics consistently before adding process-local runtime
overlays.

The process-local coordinator exists only after activation and while a
verification is active. It consumes lifecycle commits after SQLite commit and
is not a durable fact source. SQLite rows survive restart and startup
reconciliation terminalizes interrupted work. Artifact bytes remain
cleanup-safe. See the
[execution protocol](../../../../protocol/execution.md) for graph, identity,
result, and availability semantics.
