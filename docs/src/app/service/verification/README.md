# `app/service/verification`

Owns verification and custom-run identity, source signatures, execution plans, task lifecycle, result evaluation, expected-behavior matching, artifact ownership, and read models.

Inputs are an authorized workspace or frozen published snapshot, selected tests and solutions, runtime configuration, and typed execution results. Outputs are durable verification/task decisions and cache-payload ownership locators. Judgehost transport and blob storage remain separate services.

The task graph contains input generation, main-correct execution, and checked solution runs. Tasks for one program share compilation. Completion applies testcase-level allowed verdicts and program-level required verdicts before finalizing the parent. Cancellation and startup recovery terminalize the parent and open tasks atomically.

History and detail reads combine one consistent SQLite snapshot with a process-local runtime overlay. Workspace-owned records and published problem-level records have distinct visibility and cancellation rules. Rejudge creates a new verification in the viewer's current workspace.

Activation installs a process-local admission index containing canonical task
identities and metadata. Binding, exposure, and lease changes use a short memory
lock. SQLite transactions arbitrate ordinary task completions. Their post-commit
updates remove admission entries idempotently and only cache the durable result
in the existing matching execution binding. Result encoding and artifact row preparation happen before the write transaction,
for the submitted completions only. Activation also prepares initial result JSON
before entering its write transaction. Each operation retains bounded results and
their encoding by source object identity; the preparation is reused on transaction
retries. Ordinary planned tasks share one immutable empty result, and duplicate
generators retain their own skip feedback. No database work holds the memory lock.
Activation, generated-input ownership, cancellation, and fatal completion coordinate
per verification across the transaction and memory publication. Coordination is
acquired before entering SQLite and released after publication; waiting users keep
the same coordination object alive. The last completion drains owner publication
before discarding that verification's input index.
Completed tasks leave the admission index. While cancellation commits, admission
for that verification waits for its outcome. A committed cancellation removes every admission entry in its verification,
including completions whose memory publication is still pending, and rejects
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

Task-list and lifecycle-snapshot reads share immutable structured results for
identical persisted JSON within that read. Program verdict aggregation also
reuses the current transaction's prepared results and matching runtime results.
The temporary lookup contains only results encountered by the operation and is
released when it returns. Returned rows own their result objects; subsequent
reads obtain a fresh database snapshot.

Browser testcase fragments use a separate scoped read model. In one SQLite read
transaction, the service reads the parent record, invokes authorization, then
materializes persisted metadata and selected task evidence. A program selection
loads the testcase's generator, main-correct task, and selected solution; a testcase
selection loads all its programs. Duplicate generators also load their owner,
preferring a valid predecessor and then matching output in completion/id order.
Artifact references use one query ordered by task id, retaining the first owner.
The runtime overlay copies only selected and owner task identities under its lock.

Scoped models expose testcase evidence and persisted parent facts. Full models
provide task counts and program summaries for Agent and complete-page consumers.
Matrix overviews omit pass display dictionaries while retaining final-pass metric
fallbacks. Testcase details and sample JSON retain every captured pass.

Sanity admits both checker stability probes after ordinary execution completes,
runs the independent runtime, boundary, and sample-output checks, then collects
the probes. Probe diagnostics retain their planned order. Every successfully
admitted probe has program cleanup registered for normal and exceptional exit;
probe admission, result collection, and cleanup failures become check failures.

The [execution protocol](../../../../protocol/execution.md) defines lifecycle, graph, verdict, cache, and evidence semantics.
