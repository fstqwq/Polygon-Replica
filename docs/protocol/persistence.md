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

Task completion uses one `BEGIN IMMEDIATE` transaction under the verification
runtime write lock. For each task whose `final_status` is empty, that transaction
writes the terminal status, bounded canonical result, finish time, non-empty
input or answer locator, and the verification's first non-empty failure reason.
Generator content deduplication, skipped-generator results, and pending
descendant skips are included in the transaction. Any failure rolls back all of
those writes; process-local failure and task indexes are synchronized only after
the transaction commits.

An already-terminal task is not rewritten. Completion returns its persisted
result and locators as the effective state, so duplicate or conflicting
callbacks cannot attach a new locator or failure reason. Late Judgehost debug
information uses a separate amendment transaction for an existing terminal
task. It always leaves task status and input/answer locators unchanged. If no
first failure is stored, the amendment may set it; otherwise it replaces the
first failure in the same transaction only when the stored reason is the
expected reason owned by that task. A first failure owned by another task is
preserved while the canonical result evidence is amended.

Preview, export, package-build, and contest-job rows survive normal restarts.
Unfinished rows are moved to a terminal failure/cancellation state because their
process-local work cannot resume. Administrative artifact cleanup removes the
execution/package/export/build subset described by the
[storage protocol](storage.md#maintenance-cleanup), while identity, authoring,
contest source, attachment, and configuration rows remain.

## Configuration and audit

`system_config` is a key/value JSON store with update identity and timestamp. It
is the application authority for settings such as secure-cookie behavior; there
is no environment-variable override for that setting.

`audit_log` appends action, actor, optional problem, details JSON, and timestamp.
It is an operational record, not an event-sourced reconstruction of domain
state. Missing referenced identities are normalized according to the current
write service rather than blocking the audit record.

## Schema changes

At startup the application creates missing tables, runs the current narrow
contest-build nullability rebuild when required, validates that every canonical
table and required column exists, and creates missing named indexes. It does not
compare existing column constraints or existing index definitions against the
DDL. A schema change updates the DDL, required-column manifest, service queries,
cleanup policy, and this document together. No project-owned schema version is
reserved without an actual compatibility boundary.
