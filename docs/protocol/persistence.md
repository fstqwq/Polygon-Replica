# SQLite persistence

The canonical schema is the `SCHEMA` declaration and validation manifest in
`app/db.py`. SQLite does not store committed source files or large artifact
payloads. The physical table inventory is in
[the SQLite implementation map](../implementation/sqlite.md).

Foreign keys identify domain relationships. JSON columns carry structured
details whose owning service defines their shape; they are not interchangeable
with filesystem payloads.

## Execution rows

`verifications` is the durable summary for both full verification and custom
run, distinguished by `kind`. It records source/signature context, mode, pass
limit, run configuration, status, failure fields, and timestamps.

`verification_tasks` stores the task DAG and `result_json`. Generated testcase
input and answer locators are in `verification_artifact_refs`. The schema has no
`verification_tasks.output_ref` column.

Verification admission is insert-only. It creates a `queued` parent without
task rows. Activation uses one `BEGIN IMMEDIATE` transaction to compare-and-set
that parent to `running`, store its complete detail, and batch-insert the entire
validated graph. A zero-row compare-and-set reports an already-running, closed,
or missing verification; it never deletes or rewrites a graph. Each task row's
`program_id` is its durable membership in a verification program. A program
means one source path and compile specification whose test tasks share one
compilation; it is not an arbitrary task group. The accepted solution,
generator, and each checked solution have distinct program identities.

There is no separate durable program table. The immutable plan owns the program
definition, while task rows denormalize `program_id`, task kind, source path, and
expected behavior. Judgehost validates the corresponding compile identity when
cases join the program's batch. A task ID is exactly
`vt~<verification_id>~<program_id>~<test_name>`. Constructing the key performs
no database read; activation recomputes every key and rejects a mismatch,
duplicate, or inconsistent program definition as an invalid plan.

Task completion uses one `BEGIN IMMEDIATE` transaction under the verification
runtime write lock. For each task whose `final_status` is empty, that transaction
writes the terminal status, bounded canonical result, finish time, non-empty
input or answer locator, and the verification's first non-empty failure reason.
Generator content deduplication, skipped-generator results, and pending
descendant skips are included in the transaction. Generator, `main-correct`,
and cancellation failures are hard: they also change the parent to `failed`
and cancel remaining open tasks in that transaction. A solution mismatch is
soft: its first reason is persisted while other solution tasks continue; the
last task transaction changes the parent to `failed` if that reason is present.
A normal last completion otherwise finishes the parent or durably claims
sanity processing. Sanity output and its final parent compare-and-set are
committed together, so concurrent cancellation cannot be overwritten. Any
failure rolls back all writes; process-local indexes and coordinators are
synchronized only after the transaction commits.

An already-terminal task is not rewritten. Completion returns its persisted
result and locators as the effective state, so duplicate or conflicting
callbacks cannot attach a new locator or failure reason.

The Judgehost completion publisher marks an in-memory case acknowledged only
after this transaction succeeds or reports the task already terminal. A failed
transaction leaves the case unacknowledged so the callback receives non-2xx and
can be retried. Custom-run cases have no verification task identity; their
terminal result is retained by the process-local Judgehost scheduler and does
not enter this verification transaction.

`verification_task_diagnostics` stores at most one late-diagnostic snapshot per
task. Its columns are `task_id`, `snapshot_json`, and `updated_at`. A diagnostic
append uses one `BEGIN IMMEDIATE` read/merge/upsert transaction. A content digest
deduplicates retries; unchanged snapshots do not write. The bounded ordered
snapshot retains the newest items when it exceeds the auxiliary display limit.
This table is not a second completion store: appending diagnostics cannot update
`result_json`, terminal status, artifact refs, parent status, or `fail_reason`.

Cancellation and failure compare-and-set a `queued` or `running` parent to
`failed`, preserve its first non-empty reason, and cancel every open task in one
transaction. Startup recovery performs the equivalent bulk parent/task
transition before runtime blobs and Judgehost state are cleared. Recovery
failure aborts application startup.

Verification detail is read as one SQLite snapshot containing parent, detail,
tasks, refs, and late diagnostics. A process-local runtime overlay is applied
after that read and is not persisted as a competing state authority.

### Linearization guarantees

`BEGIN IMMEDIATE` serializes the aggregate writers. If activation commits before
cancellation, readers see a complete running graph and cancellation then
terminalizes it; if cancellation commits first, activation's `queued` compare-
and-set changes no row and inserts no task. Duplicate workers are the same race:
only one can perform `queued -> running`.

Task completion and cancellation likewise compete on the open task and active
parent predicates. The first writer fixes the terminal result; the second may
only observe it and cancel other open tasks. Sanity completion requires a still-
running parent, so an earlier cancellation cannot be overwritten. A crash or
statement failure during activation exposes either the complete plan or the
original taskless `queued` row, never a partial graph.

Diagnostic appends serialize their read/merge/upsert independently. They can
race with one another without a lost-update window; bounded snapshot eviction
remains the only removal rule. Their table has no write path to any decision
column. This separation makes a late diagnostic incapable of reopening
execution even when it arrives concurrently with completion or cancellation.

Preview, export, package-build, and contest-job rows survive normal restarts.
Unfinished rows are moved to a terminal failure/cancellation state because their
process-local work cannot resume. Administrative artifact cleanup removes the
execution/package/export/build subset described by the
[storage protocol](storage.md#maintenance-cleanup), while identity, authoring,
contest source, attachment, and configuration rows remain.

`exports` owns one derived cache row for each Native materialization and export
type. `export_jobs` owns request attempts: distinct requests retain distinct job
IDs even when they finish by referencing the same cached export row. Contest
label variants are contest artifacts and do not enter `exports`.

## Configuration and audit

`system_config` is a key/value JSON store with update identity and timestamp. It
is the application authority for settings such as secure-cookie behavior; there
is no environment-variable override for that setting.

`audit_log` appends action, actor, optional problem, details JSON, and timestamp.
It is an operational record, not an event-sourced reconstruction of domain
state. Missing referenced identities are normalized according to the current
write service rather than blocking the audit record.

## Schema changes

At startup the application first applies the concrete recognized table-shape
upgrades, then creates missing current objects, validates every canonical table
and required column plus the constraints owned by those upgrades, and creates
missing named indexes. The shape-upgrade owner reconstructs the historical
non-nullable `contest_build_items` table when present. It also recognizes the
historical `exports.options_hash` table, clears job references to those derived
rows, and replaces it atomically with the canonical materialization/type cache.
It does not preserve the old derived export rows.

Other existing column constraints and index definitions are not compared
against the DDL. A schema change updates the DDL, required-column manifest,
service queries, cleanup policy, and this document together. No project-owned
schema version is reserved without an actual compatibility boundary.
