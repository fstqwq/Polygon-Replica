# ucupf-3rd Cherry-pick Checklist

Source branch: `origin/ucupf-3rd`

Compared against: `main` at `9b90126`

Branch-only commits:

- [ ] `225d9e3` Integrate Qiulygon production fixes
- [ ] `04f4e73` Integrate latest Qiulygon fixes

Known merge conflict:

- [ ] `app/service/export/service.py`: conflicts with current `main` commit `9b90126` (`Fix interactive ICPC export data layout`). This must be manually reconciled if any export changes are selected.

## Branding / Product Name

- [ ] Rename UI/app brand from `Polygon-Replica` / `not polygon` to `qiulygon`.
  - Files include `app/main.py`, `app/template/base.html`, `app/template/auth_base.html`, `app/template/root_base.html`, `app/template/contest_base.html`, `app/template/_topbar_common.html`, `_terms_of_use.html`, register/login/setup templates, SMTP mail text, and related tests.
  - Product impact: visible global rename.
  - Risk: broad but mostly textual. Should not be mixed into unrelated functional cherry-picks unless we want the rename.

- [ ] Update CSS cache-busting version from `20260422-13` to `20260504-01`.
  - Files include base templates and `app/static/style.css`.
  - Product impact: makes browser pick up CSS changes from this branch.
  - Risk: only meaningful if CSS/template changes are also selected.

## Database / Migration

- [x] Add account ban fields to `users`.
  - Adds `users.is_banned INTEGER NOT NULL DEFAULT 0`.
  - Adds `users.banned_at TEXT`.
  - Updates `CURRENT_SCHEMA_COLUMNS`.
  - Files: `app/db.py`, `app/service/disk/auth_store.py`, `app/service/disk/workspace_store.py`.
  - Risk: branch currently adds `_apply_compat_migrations()` runtime migration code. This conflicts with the repo policy of not keeping compatibility migration code after live migration.

- [x] Decide migration style for ban fields.
  - Option A: keep branch runtime `_apply_compat_migrations()`.
  - Option B: do one live DB migration, then keep only final schema.
  - Selected: Option B. Runtime compatibility migration code is not kept.

## Auth / User Admin

- [x] Add system admin user management panel in Settings.
  - Search users by username/email.
  - Show active system admin count.
  - Grant/revoke system admin role.
  - Ban/unban users.
  - Reset another user's password.
  - Files: `app/impl/problem/setting.py`, `app/route/problem_route.py`, `app/template/settings.html`, `app/static/ui.js`, `app/impl/auth/password_envelope.py`, `app/service/auth/service.py`, `app/service/disk/auth_store.py`.
  - Risk: large auth surface; requires careful route authorization and session revocation review.

- [x] Add admin password-envelope scope.
  - Adds `settings-admin-password` / `settings-admin-new`.
  - Used by admin password reset.
  - Files: `app/impl/auth/password_envelope.py`, `app/impl/root/auth_pages.py`, `app/static/ui.js`.
  - Risk: security-sensitive; should be picked only with admin password reset.

- [x] Enforce banned users cannot log in.
  - Login returns `account is banned`.
  - Ban revokes auth sessions, sudo sessions, agent sessions, and agent tokens.
  - Files: `app/impl/root/auth_pages.py`, `app/service/disk/auth_store.py`.
  - Risk: depends on DB ban fields.

- [x] Allow system admins to see all problems and contests through access context.
  - Problem access role becomes `admin`.
  - `admin` has read/write/manage.
  - Agent role levels include `admin`.
  - Files: `app/service/repository/workspace.py`, `app/service/disk/workspace_store.py`, `app/impl/workspace/context_operation.py`, `app/service/agent/service.py`, contest service/store files.
  - Risk: authorization semantics change globally.

- [ ] Add system-admin username highlighting visible only to system-admin viewers.
  - New macro `_user_display.html`.
  - CSS class `.system-admin-username`.
  - Used in topbar, settings, contest access, problem access.
  - Files: `app/template/_user_display.html`, `app/static/css/10_layout.css`, related templates.
  - Risk: visual-only, but depends on passing `is_system_admin` in contexts.

- [ ] Relax registration email regex to general RFC-like email addresses.
  - Current `main` default restricts gmail/sjtu style.
  - Branch allows dots, plus tags, and general domains.
  - Files: `app/main_constant.py`, `app/template/register.html`, `tests/test_ui_auth.py`.
  - Product impact: directly changes registration policy.

## Contest Access

- [x] Add contest member to problem access sync.
  - Sync one member's contest role to all contest problems the actor can write.
  - Sync all members at once.
  - Owners and system admins are preserved.
  - Files: `app/impl/contest/access.py`, `app/route/contest_route.py`, `app/template/contest_access.html`, contest tests.
  - Risk: mutates problem ACLs from contest UI; needs clear product approval.

- [x] Add reminder after granting contest member role.
  - Grant message tells user whether problem access needs sync.
  - Files: `app/impl/contest/access.py`, `app/template/contest_access.html`.
  - Risk: low if sync feature is accepted.

## Contest Packages / Contest Statements

- [x] Add contest statement source management UI.
  - Supports language switch.
  - Edit `statements.tex`.
  - Edit `olymp.sty`.
  - Upload resources under `statements/<language>/`.
  - Download/delete stored contest statement files.
  - Files: `app/impl/contest/package.py`, `app/route/contest_route.py`, `app/template/contest_packages.html`, `app/service/contest/service.py`, `app/service/disk/contest_store.py`.
  - Risk: large new filesystem-backed contest feature.

- [x] Store contest statement sources in contest source root.
  - Adds normalization for `statements/<language>/...`.
  - Rejects unsafe paths/symlinks.
  - Normalizes text sources.
  - Files: `app/service/contest/service.py`, `app/service/disk/contest_store.py`.
  - Risk: must review path safety carefully.

- [x] Generate default contest `statements.tex`.
  - Generates problem include list based on contest problem entries and source folder map.
  - Files: `app/impl/contest/shared.py`, `app/impl/contest/package.py`.
  - Risk: output format affects contest PDF compilation.

- [x] Improve contest PDF worker.
  - Handles CJK support in contest TeX.
  - Hoists TikZ libraries and color definitions from problem statements.
  - Prepares graphics bounding boxes.
  - Writes better compile logs and failure details.
  - Files: `app/impl/contest/shared.py`.
  - Risk: large LaTeX behavior change.

- [x] Improve contest package worker reporting.
  - Selected job report can show top-level errors.
  - Per-problem rows can show warning as well as error.
  - Files: `app/impl/contest/shared.py`, `app/template/contest_packages.html`.
  - Risk: mostly UI/reporting.

- [x] Fix contest problem reorder by using `contest_problems.id`.
  - Avoids ambiguous reorder when problem IDs and contest row IDs differ.
  - Uses temporary index values to allow A/B swaps without unique-index collision.
  - Files: `app/service/disk/contest_store.py`, `app/service/contest/service.py`, contest problem route/tests.
  - Risk: worthwhile isolated bugfix.

## Export / ICPC Package

- [x] Map verification artifacts by spec test id instead of runtime ordinal.
  - Uses `verification_tests_meta.source_id` to map generated tests such as spec id `003` to runtime `002.in`.
  - Adds `_verification_artifact_ref_candidates()`.
  - Files: `app/service/export/service.py`, `app/impl/workspace/context_job.py`, `tests/test_export.py`.
  - Risk: important correctness fix; conflicts with current main export file.

- [x] Reuse existing complete ICPC verification artifacts before generating new export data.
  - Finds recent `all` or `custom` verification with complete input/answer blobs.
  - Generates only accepted-solution data when needed.
  - Files: `app/impl/workspace/context_job.py`.
  - Risk: changes export job lifecycle and verification semantics.

- [x] Generate ICPC export data with `kind=custom` and `skip_sanity=True`.
  - Runs only accepted solution to materialize input/answer artifacts for export.
  - Files: `app/impl/workspace/context_job.py`.
  - Risk: sanity does not block export data generation; should match intended package semantics.

- [x] Keep interactive samples out of DOMjudge `data/sample`.
  - Interactive tests are copied to `data/secret`.
  - Secret `.ans` files are blank for interactive.
  - Statement PDF excludes sample tests for interactive.
  - Files: `app/service/export/service.py`, `tests/test_export.py`.
  - Risk: overlaps current main `9b90126`; must preserve main's latest interactive export layout fix.

- [x] Keep multi-pass pass-fail samples out of DOMjudge `data/sample`.
  - Multi-pass samples are copied to `data/secret`.
  - Answers remain real verification answers.
  - Statement PDF excludes sample tests for multi-pass.
  - Files: `app/service/export/service.py`, `tests/test_export.py`.
  - Risk: product semantics change; verify against DOMjudge expectations.

- [x] Require interactor source for interactive ICPC export.
  - `mode == interactive` with missing interactor now fails export.
  - Files: `app/service/export/service.py`, `tests/test_export.py`.
  - Risk: stricter behavior; likely correct.

- [x] Add `include_sample_tests` parameter to statement PDF compilation during export.
  - File: `app/service/export/service.py`.
  - Risk: tied to sample placement changes.

## Preview / Statement Editor

- [x] Skip preview sample synchronization for interactive problems.
  - No sample-only verification is run for interactive preview sample sync.
  - Files: `app/service/statement/preview.py`, `tests/test_preview.py`.
  - Risk: intended if interactive samples cannot be validated through standard sample-only path.

- [x] Add endpoint to view rendered statement TeX.
  - Route: `/problems/{problem}/statement/source.tex`.
  - Renders statement assets to a temp dir and returns `statement-<language>.tex`.
  - Files: `app/impl/preview/preview.py`, `app/route/preview_route.py`.
  - Risk: read-only endpoint; still should verify access and temp cleanup.

- [x] Add "Restore default templates" action.
  - Restores `statement/statements.ftl`, `statement/problem.tex`, `statement/olymp.sty`.
  - Does not overwrite language section files.
  - Files: `app/impl/preview/preview.py`, `app/route/preview_route.py`, `app/template/preview.html`, tests.
  - Risk: destructive write action; depends on write access.

- [x] Rework preview page layout.
  - Moves preview toolbar/status above editor.
  - Moves log references and `latex.log` into diagnostics below editor.
  - Adds CSS classes in `30_forms.css`.
  - Files: `app/template/preview.html`, `app/static/css/30_forms.css`.
  - Risk: large template diff; visual regression possible.

## Judgehost / DOMjudge Compatibility

- [x] Treat `register_host` as heartbeat, not hard reconnect.
  - Stops requeueing leased jobs on every `/judgehosts` register/heartbeat.
  - Recovery remains lease expiry/disable path.
  - File: `app/service/judgehost/dispatch.py`.
  - Risk: important for avoiding duplicate delivery; verify dead-host recovery still works.

- [x] Include client port in judgehost peer binding.
  - `_request_peer_addr()` becomes `host:port` when port is known.
  - File: `app/impl/judgehost/api.py`.
  - Risk: may affect peer lookup behavior behind proxies or keep-alive.

- [x] Compact heavy judgehost task payloads after DOMjudge job preparation.
  - New `app/service/judgehost/payload_retention.py`.
  - Drops `domjudge_precomputed`, `extra_sources_b64`, `source_b64`, and heavy verification payload blobs after cache/executable materialization.
  - Files: `app/service/judgehost/task_queue.py`, `app/service/judgehost/dispatch.py`, `app/service/judgehost/payload_retention.py`.
  - Risk: must ensure later result/debug paths do not require removed payload fields.

- [x] Prioritize `generate-input` tasks over solution runs.
  - Adds priority sort for queued tasks/jobs.
  - Files: `app/service/judgehost/task_queue.py`, `app/service/memory/judgehost_state_store.py`, tests.
  - Risk: scheduler behavior change; intended for DAG throughput.

- [x] Defer priority preemption until current leased case reports.
  - Prevents immediate host reassignment while a case is still in flight.
  - Files: `app/service/judgehost/task_queue.py`, `app/service/memory/judgehost_state_store.py`.
  - Risk: important for judgehost stability.

- [x] Share compiled DOMjudge job pending cases across hosts.
  - Allows a compiled job to lease remaining pending cases to another host.
  - Files: `app/service/memory/judgehost_state_store.py`, `app/service/judgehost/task_queue.py`, tests.
  - Risk: must ensure one case is not leased twice.

- [x] Reuse existing job when second task shares run id.
  - Appends cases to existing job instead of creating duplicate job when run id matches.
  - Files: `app/service/judgehost/dispatch.py`, `app/service/memory/judgehost_state_store.py`.
  - Risk: touches grouped job semantics.

- [x] Add/get executable file lookup changes from branch.
  - Branch contains additional changes in `app/service/judgehost/result.py` and state store script-hash queries.
  - Main already has a recent shared executable cache fix; compare carefully to avoid regressing it.
  - Files: `app/service/judgehost/result.py`, `app/service/memory/judgehost_state_store.py`.
  - Risk: high because this recently fixed live judgehost 404 loops.

## Verification DAG / Scheduler

- [x] Wait for late verification artifact visibility.
  - `_verification_required_blob()` retries until artifact ref and blob are visible.
  - Files: `app/impl/workspace/verification_dag.py`, `tests/test_verification_task_scheduler.py`.
  - Risk: low-to-medium; changes timeout behavior.

- [x] Publish and actively probe ready verification tasks in 32-Case slices.
  - Runtime identity is registered before cache-hit events are emitted.
  - Removes the unrelated legacy publish-count cap while yielding between probe slices.
  - Files: `app/service/verification/task_scheduler.py`, tests.
  - Risk: scheduler behavior change; relevant to stuck generating cases.

- [x] Add verification DAG plan metadata for export/sample behavior.
  - Files: `app/impl/workspace/verification_dag_plan.py`, `app/impl/workspace/verification_dag.py`.
  - Risk: verify actual changes before picking independently.

## Workspace / Git / Problem Navigation

- [x] Avoid computing full workspace diff in `git_service.status`.
  - Status returns changed paths without full diff.
  - Tests assert `git diff` is not called for status.
  - Files: `app/service/repository/git.py`, tests.
  - Risk: performance improvement; ensure callers do not expect `status["diff"]`.

- [x] Do not auto-select first changed file diff on workspace page.
  - Workspace page only computes diff when user requests a path.
  - Files: `app/impl/workspace/context_ui.py`, tests.
  - Risk: UI behavior change; improves large workspace performance.

- [x] Improve switch-workspace behavior for leaf slugs.
  - If user enters `leaf` and exactly one accessible foreign `owner/leaf` exists, open it.
  - If a foreign `leaf` exists but is not accessible, require full problem id instead of creating duplicate.
  - Explicit `owner/leaf` still creates/opens requested problem if allowed.
  - Files: `app/impl/problem/workspace_op.py`, workspace service/store, tests.
  - Risk: product behavior change; likely reduces accidental duplicates.

- [x] Ensure existing workspace fast path switches to `main`.
  - Calls `_ensure_main_checkout()` before refreshing status in fast path.
  - Files: `app/service/repository/workspace.py`.
  - Risk: may affect users with local branch state; current model expects `main`.

- [x] Improve unborn/empty repo main checkout handling.
  - Fetches origin main if needed, else keeps symbolic HEAD on main.
  - Files: `app/service/repository/workspace.py`.
  - Risk: mostly bootstrap correctness.

## Solutions

- [x] Remove synchronous compile check on solution source save.
  - Deletes `judgehost_compile_check_error()` call and rollback.
  - Files: `app/impl/problem/solution.py`.
  - Product impact: solution editor save no longer blocks on compile check.
  - Risk: intentional behavior change; should be decided explicitly.

## Artifact Access

- [x] Allow verification artifact access by verification id for same problem.
  - Existing `p-...` artifact id path remains.
  - Non-`p-` id is accepted if a verification record exists for same problem.
  - File: `app/impl/workspace/artifact.py`.
  - Risk: broadens artifact access path; should review workspace/user relationship.

## Docker / Deployment

- [x] Add `texlive-plain-generic` to Dockerfile.
  - Intended to fix LaTeX package availability in container.
  - File: `Dockerfile`.
  - Risk: low.

## Tests

- [x] Add backend minimal tests.
  - File: `tests/test_backend_minimal.py`.
  - Includes many coverage additions for branch behavior.

- [x] Add/extend export tests.
  - File: `tests/test_export.py`.
  - Covers artifact mapping, multi-pass sample placement, interactive export requirements.

- [x] Add/extend judgehost tests.
  - File: `tests/test_judgehost_service.py`.
  - Covers host register heartbeat, job sharing, priority, preemption, executable lookup behaviors.

- [x] Add/extend preview tests.
  - File: `tests/test_preview.py`.
  - Covers interactive sample sync skip.

- [x] Add/extend UI auth/settings tests.
  - File: `tests/test_ui_auth.py`.
  - Covers admin user management, ban, password reset, email regex changes.

- [x] Add/extend contest tests.
  - File: `tests/test_ui_contests.py`.
  - Covers contest access sync, statement sources, packages, reorder.

- [x] Add/extend workspace tests.
  - File: `tests/test_ui_workspace.py`.
  - Covers workspace switch leaf behavior, no auto diff, statement template reset.

- [x] Add/extend verification scheduler tests.
  - File: `tests/test_verification_task_scheduler.py`.
  - Covers late artifact visibility and ready batch publication.

- [x] Add/extend run page tests.
  - File: `tests/test_ui_run.py`.
  - Covers selected verification/export/run UI behavior from the branch.

- [x] Add shared UI test support changes.
  - File: `tests/ui_support.py`.
  - Supports the selected auth/contest/workspace UI tests.

## Suggested Isolated Cherry-pick Groups

- [ ] Group A: Low-risk isolated fixes.
  - Docker `texlive-plain-generic`.
  - Contest reorder unique-index fix.
  - Workspace page no auto diff.
  - Git status no full diff.

- [ ] Group B: Export correctness.
  - Verification artifact mapping by spec id.
  - Interactive/multi-pass sample placement.
  - Strict interactive interactor requirement.
  - Must manually resolve `app/service/export/service.py` with `main` `9b90126`.

- [ ] Group C: Judgehost stability.
  - Heartbeat register behavior.
  - Payload retention.
  - Task priority/preemption.
  - Shared pending cases across hosts.
  - Must avoid regressing main shared executable cache fix.

- [ ] Group D: Contest authoring features.
  - Contest statement source management.
  - Contest PDF worker improvements.
  - Contest package report improvements.

- [ ] Group E: Admin/security model.
  - Ban fields.
  - Admin user management.
  - Global system admin access.
  - Agent `admin` role.
  - Requires DB migration decision.

- [ ] Group F: Statement/preview authoring.
  - Rendered source endpoint.
  - Restore default templates.
  - Preview layout rework.
  - Interactive sample sync skip.

- [ ] Group G: Branding.
  - Full `qiulygon` rename.
  - Terms/mail/title/template test updates.
