# `app/service/verification`

Owns verification and custom-run identity, source signatures, execution plans, task lifecycle, result evaluation, expected-behavior matching, artifact ownership, and read models.

Inputs are an authorized workspace or frozen published snapshot, selected tests and solutions, runtime configuration, and typed execution results. Outputs are durable verification/task decisions and cache-payload ownership locators. Judgehost transport and blob storage remain separate services.

The task graph contains input generation, main-correct execution, and checked solution runs. Tasks for one program share compilation. Completion applies testcase-level allowed verdicts and program-level required verdicts before finalizing the parent. Cancellation and startup recovery terminalize the parent and open tasks atomically.

History and detail reads combine one consistent SQLite snapshot with a process-local runtime overlay. Workspace-owned records and published problem-level records have distinct visibility and cancellation rules. Rejudge creates a new verification in the viewer's current workspace.

The [execution protocol](../../../../protocol/execution.md) defines lifecycle, graph, verdict, cache, and evidence semantics.
