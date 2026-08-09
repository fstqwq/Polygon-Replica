# SQLite persistence

The canonical schema is the `SCHEMA` declaration and validation manifest in
`app/db.py`. SQLite does not store committed source files or large artifact
payloads.

## Durable groups

- Identity and access: users, browser auth/sudo sessions, registration and rate
  limit state, agent sessions/tokens/access requests, and repository ACLs.
- Authoring: problems and workspaces.
- Contests: contests, membership, problem membership, jobs, build items,
  artifacts, and attachments.
- Execution: previews, verifications, selected tests, source paths, sanity
  checks/messages, test metadata, task DAG rows, and artifact references.
- Packaging: materializations, builds, exports, and export jobs.
- Operations: audit log, system configuration, and singleton SMTP configuration.

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
process-local work cannot resume.

## Configuration and audit

`system_config` is a key/value JSON store with update identity and timestamp. It
is the application authority for settings such as secure-cookie behavior; there
is no environment-variable override for that setting.

`audit_log` appends action, actor, optional problem, details JSON, and timestamp.
It is an operational record, not an event-sourced reconstruction of domain
state. Missing referenced identities are normalized according to the current
write service rather than blocking the audit record.

## Schema changes

The application validates the expected columns and indexes on startup. A schema
change updates the DDL, validation manifest, service queries, cleanup policy,
and this document together. No project-owned schema version is reserved without
an actual compatibility boundary.
