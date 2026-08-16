# SQLite implementation map

The physical DDL and required-column manifest are maintained together in
`app/db.py`. SQLite uses WAL journaling and incremental auto-vacuum.

| Responsibility | Tables |
| --- | --- |
| authentication | `users`, `auth_sessions`, `sudo_sessions`, `pending_registrations`, `auth_rate_limits` |
| agent access | `agent_registration_codes`, `agent_sessions`, `agent_access_requests`, `agent_problem_grants` |
| authoring | `problems`, `repo_acl`, `workspaces` |
| contests | `contests`, `contest_members`, `contest_problems`, `contest_jobs`, `contest_build_items`, `contest_artifacts`, `contest_attachments` |
| execution | `previews`, `verifications`, `verification_selected_tests`, `verification_source_paths`, `verification_sanity_checks`, `verification_sanity_check_messages`, `verification_tests_meta`, `verification_tasks`, `verification_task_artifacts`, `verification_task_diagnostics` |
| packages | `problem_package_materializations`, `problem_package_builds`, `exports`, `export_jobs` |
| configuration | `system_config`, `smtp_config` |

Important physical facts:

- `workspaces` is unique per problem/user and stores checkout/status projections.
- `verifications` represents full verification and custom run through `kind`.
- `verification_tasks.result_json` owns structured task results.
- `verification_tasks.program_id` records the program whose source and compile
  specification are shared by that program's test tasks; task IDs are the
  natural key `vt~<verification_id>~<program_id>~<test_name>`.
- `verification_task_artifacts` indexes every canonical execution cache payload
  plus generated input and accepted answer ownership. It is keyed by task, pass,
  and role and stores the runtime ref and server-chosen download filename. Its
  composite foreign key requires the task to belong to the same verification.
- `verification_task_diagnostics` has one bounded, retry-deduplicated late
  diagnostic snapshot per task; it does not amend `result_json`.
- physical materializations are unique by problem/source commit; problem-level
  projections are unique by materialization and external format.
- `system_config` is mutable key/value JSON and `smtp_config` is a singleton.

Startup initializes only an absent or empty database. For an existing database,
it performs a read-only check for every canonical table, required column, and
named index. Missing objects block application runtime and are listed in a raw
operator-facing `503`; no startup path creates, alters, rebuilds, or drops an
existing object. Extra objects and rows are tolerated and preserved.
