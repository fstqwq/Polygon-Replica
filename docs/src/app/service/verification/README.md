# `app/service/verification`

Owns verification and custom-run identity, source signatures, execution plans, task lifecycle, result evaluation, expected-behavior matching, artifact ownership, and read models.

Inputs are an authorized workspace or frozen published snapshot, selected tests and solutions, runtime configuration, and typed execution results. Outputs are durable verification/task decisions and cache-payload ownership locators. Judgehost transport and blob storage remain separate services.

The task graph contains input generation, main-correct execution, and checked solution runs. Tasks for one program share compilation. Completion applies testcase-level allowed verdicts and program-level required verdicts before finalizing the parent. Cancellation and startup recovery terminalize the parent and open tasks atomically.

History and detail reads combine one consistent SQLite snapshot with a process-local runtime overlay. Workspace-owned records and published problem-level records have distinct visibility and cancellation rules. Rejudge creates a new verification in the viewer's current workspace.

Activation installs a process-local admission index containing canonical task
identities and metadata. Binding, exposure, and lease changes use a short memory
lock. Completion transactions use a separate commit lock to order durable-result
and input-owner cache updates; SQLite work leaves the memory lock available.
Completed tasks leave the admission index. While cancellation commits, admission
for that verification waits for its outcome. A committed cancellation rejects
waiting admissions; rollback lets them continue. Fatal completions and duplicate-input
subtree skipping also pause admission while they decide the affected tasks.
Startup recovery
and runtime reset discard the index with the other process-local state.

The task store retains generated-input owners by content-addressed output reference
within each active verification. Completion publishes new owners after its database
transaction commits; missing runtime indexes rebuild from durable results. Graph
completion, terminalization, and runtime reset discard the owner index. Bound tasks
retain their committed typed result for reads of the same persisted JSON, and release
it with their runtime binding.

The [execution protocol](../../../../protocol/execution.md) defines lifecycle, graph, verdict, cache, and evidence semantics.
