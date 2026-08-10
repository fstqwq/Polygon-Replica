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
