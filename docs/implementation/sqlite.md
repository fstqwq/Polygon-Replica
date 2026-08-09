# SQLite implementation map

The physical DDL and required-column manifest are maintained together in
`app/db.py`. SQLite uses WAL journaling and incremental auto-vacuum.

| Responsibility | Tables |
| --- | --- |
| authentication | `users`, `auth_sessions`, `sudo_sessions`, `pending_registrations`, `auth_rate_limits` |
| agent access | `agent_registration_codes`, `agent_sessions`, `agent_access_requests`, `agent_tokens` |
| authoring | `problems`, `repo_acl`, `workspaces` |
| contests | `contests`, `contest_members`, `contest_problems`, `contest_jobs`, `contest_build_items`, `contest_artifacts`, `contest_attachments` |
| execution | `previews`, `verifications`, `verification_selected_tests`, `verification_source_paths`, `verification_sanity_checks`, `verification_sanity_check_messages`, `verification_tests_meta`, `verification_tasks`, `verification_artifact_refs` |
| packages | `problem_package_materializations`, `problem_package_builds`, `exports`, `export_jobs` |
| operations | `audit_log`, `system_config`, `smtp_config` |

Important physical facts:

- `workspaces` is unique per problem/user and stores checkout/status projections.
- `verifications` represents full verification and custom run through `kind`.
- `verification_tasks.result_json` owns structured task results; the table has
  no physical `output_ref` column.
- `verification_artifact_refs` is keyed by verification/test and stores
  `input_ref` and `answer_ref`.
- materializations are unique by problem/source commit; exports are unique by
  materialization/type/options hash.
- `system_config` is mutable key/value JSON, `smtp_config` is a singleton, and
  `audit_log` is append-oriented evidence rather than event-sourced state.

Startup creates missing canonical objects and validates required schema shape.
The schema still contains an inline rebuild path for an older contest-build
column shape; this is recorded as technical debt in the findings ledger.
