# `app/service/verification`

Owns verification and custom-run identities, source manifests and signatures,
workspace snapshot acquisition, execution plans, task persistence, DAG
scheduling, result evaluation, expected-behavior matching, and read models. It
consumes a workspace identity or an explicitly frozen published-revision
snapshot, selected solutions/tests, canonical runtime configuration, and typed
Judgehost terminal reports containing an
[`ExecutionResult`](../execution/README.md). It
produces durable verification/task rows and an indexed owner row for every
runtime blob locator; it does not own the Judgehost wire protocol or blob
filesystem. Cache-payload authorization and virtual-path resolution query that
owner index and then require a currently available runtime blob descriptor;
they never reconstruct ownership by scanning result JSON.

`VerificationExecutionPlanner` turns a frozen source snapshot into the one
canonical execution plan. `VerificationWorkflow.run_workspace()` creates and
owns the service snapshot before admission; all workflow entry points own
activation, task publication, coordinator execution, sanity finalization, and
process-local cleanup through injected Judgehost, workspace, storage,
configuration, and blob ports. Pure graph construction and result summarization live in
`workflow_policy`; HTTP implementation modules do not own or import these
policies.

The lifecycle service admits a verification once, atomically activates one
immutable execution plan, commits first-wins task decisions, finalizes sanity,
and terminalizes cancellation or restart interruption. The completion boundary
validates required generator and accepted-solution outputs, applies a checked
solution's allowed verdicts per testcase, and applies its required verdicts only
after all durable tasks for that program are terminal. It commits the task
result, program-level mismatch, dependent locator, parent transition, and
remaining-task cancellation together. It accepts Judgehost publication through
narrow completion and diagnostic sinks and has explicit task-store and
runtime-blob dependencies; it does not query global runtime configuration.

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

Workspace mutation and visibility are separate scopes. Cancellation and
workspace-specific execution lookup require a record owned by that workspace.
History, detail reads, and readiness may also see problem-level published
verifications (`workspace_id IS NULL`); their original records cannot be
cancelled or amended from a workspace. Workspace history and readiness exclude
records owned by another workspace. A collaborator with problem access can
open such a record by ID for review, but cannot cancel it. Rejudge does not
mutate the source record: an
authorized viewer may use its reusable solution paths to create a new
verification owned by the viewer's current workspace.

The process-local coordinator is constructed only after activation. It normally
exists while a verification is active; a post-registration snapshot may instead
make it consume one closed-state reconciliation and unregister immediately. It
consumes lifecycle commits after SQLite commit and is not a durable fact source.
Failed completion-event delivery falls back to a durable task-snapshot
reconciliation; idle coordinators repeat that reconciliation before advancing
successors. An idle coordinator that discovers a terminal durable parent drains
Judgehost execution before it retires.
SQLite rows survive restart and startup reconciliation terminalizes interrupted
work. Cache payloads remain disposable. See the
[execution protocol](../../../../protocol/execution.md) for graph, identity,
result, and availability semantics.

`VerificationJudgehostAdapter` is the sole verification persistence boundary
presented to Judgehost. It reads the selected test refs and run configuration
as one SQLite snapshot, validates every opaque `CaseBinding` against the
durable task identity, and delegates completion, diagnostic, lease, and quiet
cleanup effects to their verification owners.

An instance-owned runtime registry admits one coordinator per verification and
requires object-identical unregistration. `VerificationExecutionService` owns
coordinator construction, registration, post-registration durable-state
reconciliation, execution, scheduler failure, cancellation, and Judgehost drain
ordering. Workspace adapters provide plans, publishers, and sanity callbacks;
completion and Judgehost lease events enter through injected narrow methods.
