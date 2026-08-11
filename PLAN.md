# Parallel Refactoring Plan

## Status and baseline

This pass is implemented on `main` through `90fae55`. Linux acceptance remains
pending; no test suite was run on Windows. The previous three-batch plan is
complete and is not an implementation target for this pass.

All implementation worktrees start from:

```text
7cebb3acb36b0bfb0e01ebee8a5f2eb012174c8d
```

The coordinator used the `main` worktree. Three implementation agents used
dedicated branches and worktrees without editing another agent's worktree.

## Delivery record

- Runtime lifecycle: `9e887e5`, `a585c9e`.
- Judgehost callback ingestion: `618ee2c`, `f50ae56`, `0aa53cd`, `a7bf5f3`.
- Canonical package identity and SQLite cleanup: `5fa8732`, `8273f77`,
  `72f8a34`.
- Rebased deployed mock-Judgehost E2E range: `542a4da` through `90fae55`.
- Coordinator plan checkpoint: `7dc9690`.

Independent review findings were fixed before integration. Per the final
operator decision, the Docker mock asserts the project-owned Judgehost protocol
and does not clone or approve pinned upstream DOMjudge source.

## Canonical decisions

The following decisions are fixed for this pass.

### Package and job identity

```text
Published source identity
  = problem_id + published source_commit

Problem export artifact identity
  = published source identity + export_type

Export attempt identity
  = export_job_id

Worker execution identity
  = export-job:{export_job_id}

Contest package identity
  = contest job + frozen contest problem + contest label
```

- `problem_package_builds` is one mutable build-state row per published source
  identity. It is not an attempt-history table. Its existing unique constraint
  and retry-in-place behavior remain unchanged.
- Distinct export requests retain distinct `export_job_id` values. A successful
  attempt may reference an already available artifact. Artifact publication and
  export-job status are independent writes: if a valid canonical artifact is
  published before a later job-link or status write fails, the artifact remains
  reusable and a later cache hit counts as a successful attempt.
- `domjudge_short_name` is contest placement metadata. It is not part of the
  problem export artifact identity and must not affect a problem export cache
  key.
- A problem-level ICPC package is canonical and uses the public problem slug as
  its legacy DOMjudge `short-name`.
- A contest build consumes the canonical package, changes contest-owned metadata
  in its own staging tree, and publishes the changed ZIP only as a contest
  artifact.
- There is no remaining configurable export option. `options_hash` must be
  removed instead of retained as a constant or hidden cache salt.

### Database and compatibility

- The current SQLite schema is authoritative.
- A concrete old table shape may have one explicit structural upgrade. Do not
  add a project schema version, compatibility wrapper, or generic migration
  framework.
- Export rows and export archives are derived cache data. The `options_hash`
  shape upgrade may invalidate all existing export rows. Historical export job
  rows remain, with unavailable artifact references cleared.
- Old unreferenced export files need not be recovered. They remain inaccessible
  and are removed by the existing exclusive artifact cleanup.
- `workspaces.recent_verification_status` is logically deleted. New databases do
  not contain it. Existing databases may retain the unused extra column; the
  application must not read, write, clear, or validate it.

### Judgehost and runtime

- Judgehost HTTP routes, authentication, callback acknowledgements, result
  normalization, artifact references, and late-diagnostic behavior do not
  change.
- Application startup and shutdown belong to runtime composition, not the
  private authentication implementation package.

## Worktree allocation

| Agent | Branch | Worktree | Responsibility |
| --- | --- | --- | --- |
| Coordinator | `main` | `C:\code\Polygon-Replica\Polygon-Replica` | Integration, `PLAN.md`, findings ledger, final verification |
| Agent 1: Package and SQLite | `codex/package-artifact-identity` | `C:\code\Polygon-Replica\worktrees\package-artifact-identity` | Canonical problem exports, contest metadata rewrite, export schema, obsolete workspace field |
| Agent 2: Judgehost ingestion | `codex/judgehost-callback-ingestion` | `C:\code\Polygon-Replica\worktrees\judgehost-callback-ingestion` | Split callback ingestion responsibilities without behavior changes |
| Agent 3: Runtime lifecycle | `codex/runtime-lifecycle` | `C:\code\Polygon-Replica\worktrees\runtime-lifecycle` | Move process lifecycle out of private auth implementation |
| E2E integration | `tests/e2e-mock` | `C:\code\Polygon-Replica-e2e-mock` | Rebase the deployed mock workflow and contest PDF journey onto the integrated implementation |

The worktrees and branches already exist at the baseline commit above.

## Shared coordination rules

- Agents may modify only the files assigned to their workstream plus directly
  owning documentation and existing tests.
- Only the coordinator edits `PLAN.md`, `docs/implementation/findings.md`, test
  resource manifests, CI workflows, import-policy configuration, or repository
  policy files.
- Do not add compatibility aliases, old-signature wrappers, schema versions,
  cache salts, or placeholder option fields.
- Extend existing test modules. Do not add a test module merely to isolate one
  assertion, and do not add UI markup assertions for internal refactors.
- Each agent leaves a clean worktree with the requested atomic commits. Agents
  do not merge, push, or rebase against a moving `main` unless the coordinator
  explicitly asks.
- No tests run on Windows. The Linux host and virtualenv must be confirmed with
  the user immediately before test execution.

## Agent 1: Canonical package artifacts and SQLite cleanup

### Objective

Make the problem export cache own exactly one artifact for each
`materialization_id + export_type`. Move contest-label rewriting into the
contest build. Remove dead SQLite identity and workspace fields.

### File ownership

Primary production ownership:

- `app/db.py` and a dependency-light adjacent SQLite shape-upgrade module if
  separation is needed;
- `app/service/disk/export_store.py`;
- `app/service/export/`;
- the contest package-building portion of `app/impl/contest/shared.py`;
- a new contest-owned metadata helper under `app/service/contest/`;
- `app/service/platform/maintenance.py` only for removal of
  `recent_verification_status` handling.

Owning tests and documentation:

- `tests/test_database_service.py`;
- `tests/test_export.py`;
- `tests/test_export_service.py`;
- `tests/test_icpc_export_package.py`;
- `tests/test_contest_builds.py`;
- `tests/test_artifact_cleanup.py`;
- `docs/protocol/package.md`;
- `docs/protocol/persistence.md`;
- `docs/src/app/service/export/README.md`;
- `docs/src/app/service/contest/README.md`;
- `docs/src/app/service/disk/README.md`.

Agent 1 must not modify Judgehost, runtime lifecycle, `PLAN.md`, or the findings
ledger.

### Change 1: Canonical problem export

- Remove the `domjudge_short_name` parameter from the public and internal
  problem export APIs.
- Derive the canonical legacy DOMjudge `short-name` only from the public problem
  slug.
- Reduce the in-process conversion lock identity from
  `materialization_id + export_type + short_name` to
  `materialization_id + export_type`.
- Remove `_options_hash()` and all option-hash arguments from export service and
  store methods.
- Change the durable uniqueness rule from
  `UNIQUE(materialization_id, export_type, options_hash)` to
  `UNIQUE(materialization_id, export_type)`.
- Preserve `export_job_id`, worker dedupe keys, job status transitions, audit
  attribution, filenames, archive integrity checks, and cache-hit behavior.

### Change 2: Contest-owned metadata rewrite

- The contest package worker first requests the canonical ICPC export without a
  contest label.
- Copy and safely extract that ZIP into the contest job staging tree.
- Rewrite exactly the `short-name` entry in `domjudge-problem.ini` to the frozen
  contest label.
- Require one valid metadata file and one `short-name` entry. Reject duplicate,
  missing, multi-line, or unsafe values.
- Preserve `problem.yaml`, `externalid`, UUID, version, color, limits,
  statements, validators, submissions, tests, and attachments byte-for-byte
  except for archive-container effects and the intended INI line.
- Repack the staged tree into the existing contest package filename and include
  it in the contest package bundle.
- Do not insert the rewritten ZIP into `exports`; it is owned by the contest job
  and contest artifact lifecycle.
- Use bounded streaming and existing safe archive helpers. Do not load a whole
  problem ZIP into memory.

### Change 3: Concrete export schema upgrade

- Detect the concrete old `exports.options_hash` shape structurally; do not add
  a schema version.
- Clear `export_jobs.export_id` references to the old derived cache records.
- Recreate `exports` with the current columns and the two-column uniqueness
  rule. Do not preserve old problem or contest variants.
- Keep historical export jobs and materializations.
- Ensure transaction rollback restores the entire old database shape if the
  table replacement fails.
- Move the existing `contest_build_items` nullability reconstruction out of the
  normal schema declaration path into the same explicit, dependency-light shape
  upgrade owner. Preserve its behavior and foreign-key validation.
- The normal path remains: apply concrete shape upgrades, create missing current
  objects, validate current required columns and constraints, then create
  indexes.
- Unreferenced old export files are not served and remain eligible for the
  existing exclusive artifact cleanup.

### Change 4: Remove the obsolete workspace field

- Remove `recent_verification_status` from the current `workspaces` DDL and
  required-column manifest.
- Remove maintenance writes and counts for the field.
- Remove tests that exist only to populate or clear it.
- Do not rebuild existing `workspaces` tables solely to remove the physical
  column. Extra columns in an existing current database remain tolerated.

### Agent 1 invariants

- A materialization has at most one `native` and one `icpc` problem export row.
- Contest labels never affect problem export identity or problem export bytes.
- Two contest labels may produce different contest ZIPs from the same canonical
  problem export.
- Distinct export jobs remain distinct even when they point to one artifact.
- A failed export attempt does not delete a valid canonical artifact.
- A schema upgrade never leaves a half-rebuilt table or broken foreign key.

### Agent 1 tests

- Repeated canonical export returns the same export ID and archive.
- Native and ICPC remain separate identities.
- Two contest builds using labels `A` and `E` reuse one canonical ICPC export but
  contain their respective `short-name` values.
- The canonical archive remains unchanged after contest packaging.
- Contest rewriting rejects malformed or duplicate INI metadata.
- A database with the old `options_hash` schema upgrades atomically, preserves
  jobs/materializations, clears stale artifact references, and accepts a new
  canonical export.
- A forced failure during table replacement rolls back fully.
- Fresh schema contains neither `options_hash` nor
  `recent_verification_status`.
- A current database with an extra legacy workspace column still starts.
- Existing artifact cleanup recreates the current export schema and no longer
  touches workspace verification status.

### Agent 1 commits

Produce two commits in this order:

```text
Make problem export artifacts canonical
Remove obsolete workspace verification status
```

The first commit includes the export shape upgrade and contest rewrite so that
every commit is runnable. The second commit contains only the dead workspace
field removal.

## Agent 2: Judgehost callback ingestion boundaries

### Objective

Reduce `app/service/judgehost/result.py` to callback orchestration. Extract
artifact capture, diagnostic payload parsing, and toolchain telemetry into
dependency-light owners without changing protocol behavior.

### File ownership

Primary production ownership:

- `app/service/judgehost/result.py`;
- new narrowly named modules under `app/service/judgehost/`;
- `app/service/judgehost/toolchain_versions.py` only where telemetry ownership
  naturally belongs.

Owning tests and documentation:

- `tests/test_judgehost_payload.py`;
- `tests/test_judgehost_host_telemetry.py`;
- `tests/test_judgehost_lifecycle.py`;
- `tests/test_judgehost_service.py`;
- `docs/protocol/judgehost.md`;
- `docs/src/app/service/judgehost/README.md`.

Agent 2 must not modify `app/db.py`, export/contest code, runtime lifecycle,
`PLAN.md`, or the findings ledger.

### Change 1: Diagnostic payload parser

- Extract parsing and normalization of `full_debug`, `output_run`, and internal
  error text.
- Accept already bounded protocol inputs and return a typed canonical diagnostic
  value.
- Keep size limits, newline behavior, digest/deduplication inputs, and late
  diagnostic classification unchanged.
- The parser must not import SQLite, runtime composition, BatchScheduler, or
  verification services.

### Change 2: Artifact capture

- Extract upload-field validation and streaming capture into a component that
  returns typed artifact refs and capture warnings.
- Keep existing locator formats, truncation rules, output selection, pass
  ordering, and runtime blob ownership.
- Artifact capture does not choose verdicts, acknowledge callbacks, publish
  completions, or update coordinators.

### Change 3: Toolchain telemetry

- Move compiler/runner telemetry extraction and recording behind one narrow
  owner.
- Keep Judgehost reports trusted after protocol authentication.
- Do not add consistency gates, cache identity fields, versions, or rejection
  behavior.

### Change 4: ResultProcessor orchestration

`ResultProcessor` retains only this order:

```text
callback admission
  -> immutable case receipt
  -> owner/generation validation
  -> scheduler claim
  -> artifact capture
  -> diagnostic parsing and telemetry
  -> existing result normalization
  -> durable completion or diagnostic publication
  -> batch finalization
  -> receipt release
  -> existing HTTP acknowledgement
```

No extracted component may return an HTTP response or directly notify a
verification coordinator.

### Agent 2 invariants

- Successful and idempotent final callbacks still return JSON integer `1`.
- Invalid owner, generation, payload, or persistence failures remain non-2xx as
  currently documented.
- Canonical completion remains first-wins.
- Late diagnostics never change verdict, refs, parent status, or successor
  release.
- Multi-pass evidence, warnings, compile data, usage, and artifact refs survive
  unchanged.
- Cleanup receipt and maintenance admission ordering do not change.

### Agent 2 tests

- Existing result-normalization matrices remain unchanged.
- Compile failure, RE, internal error, late debug, duplicate callback, and
  persistence retry retain their observable results.
- Artifact capture failure remains retryable and does not acknowledge early.
- Toolchain telemetry still records authenticated compiler/runner versions but
  never blocks completion.
- Static dependency tests prove the new parser has no DB, runtime, scheduler, or
  verification imports.
- No test asserts private call counts merely to prove the split.

### Agent 2 commit

Produce one commit:

```text
Separate Judgehost callback ingestion
```

## Agent 3: Runtime lifecycle ownership

### Objective

Resolve the top-level dependency on a private authentication module by moving
application startup and shutdown to runtime composition. Do not move the full
`RuntimeConfig` or change startup behavior.

### File ownership

Primary production ownership:

- `app/impl/auth/internal/runtime.py`;
- a new `app/impl/runtime/lifecycle.py`;
- `app/main.py`;
- runtime lifecycle imports only.

Owning tests and documentation:

- `tests/test_runtime_startup_e2e.py`;
- `docs/operations/runtime.md`;
- `docs/src/app/impl/README.md`.

Agent 3 must not modify `app/impl/runtime/config.py`, Judgehost internals,
export/contest code, `app/db.py`, `PLAN.md`, or the findings ledger.

### Changes

- Move startup recovery, interrupted-job failure, Judgehost cancellation,
  runtime cache clearing, worker start, worker stop, and their private helpers
  unchanged into `app.impl.runtime.lifecycle`.
- Update `app/main.py` to import the public runtime lifecycle owner.
- Update runtime-startup tests to patch the new owner.
- Delete `app/impl/auth/internal/runtime.py`; do not leave a re-export or
  compatibility wrapper.
- Keep auth middleware, sessions, routes, and authentication policy untouched.

### Agent 3 invariants

- Database initialization and startup recovery run in the same order.
- Runtime blobs, worker history, and startup-cleared caches retain current
  behavior.
- Interrupted package, export, preview, contest, verification, and Judgehost
  work retains current terminal handling.
- WorkerQueue starts once and stops once through the FastAPI lifespan.
- Importing `app.main` exposes the same HTTP application and routes.

### Agent 3 tests

- Existing startup recovery scenarios pass after importing the new owner.
- A startup-recovery failure still prevents application startup at the same
  boundary.
- Shutdown still stops the worker service.
- Static search finds no production import of
  `app.impl.auth.internal.runtime`.
- Import policy and cross-package private-import checks pass.

### Agent 3 commit

Produce one commit:

```text
Move application lifecycle out of auth
```

## Coordinator responsibilities

The coordinator does not implement production changes while the three agents
are working. It owns integration and shared records.

### Before integration

- Confirm every agent started from the recorded baseline.
- Reject changes outside assigned ownership unless the agent reports the reason
  before editing.
- Keep the `main` worktree limited to this plan and integration-only changes.

### Integration order

Integrate complete commits, never copy uncommitted files between worktrees:

1. `Move application lifecycle out of auth`
2. `Separate Judgehost callback ingestion`
3. `Make problem export artifacts canonical`
4. `Remove obsolete workspace verification status`

If `main` moves before integration, the coordinator rebases or cherry-picks one
branch at a time against the new baseline and resolves conflicts using current
`main` behavior. Agents must not independently race to rebase their branches.

### Findings and documentation reconciliation

After code integration, the coordinator updates the findings ledger:

- remove `PKG-005` because its job-dedupe premise was incorrect, not because
  jobs were coalesced;
- remove `STO-005` after the obsolete workspace field is gone;
- remove or narrow `STO-003` according to the final explicit shape-upgrade
  owner;
- remove or narrow `PLC-007` according to the remaining ResultProcessor
  responsibilities;
- remove `PLC-011` after `app.main` no longer imports private auth runtime code.

The coordinator then updates this plan with commit SHAs and actual acceptance
results.

## Verification

### Per-agent checks

Each agent performs, in the user-confirmed Linux environment:

- tests for its assigned existing modules;
- syntax, pyflakes, pylint for changed application modules;
- import-policy, cross-package private-import, and test-resource checks;
- `git diff --check`.

Agent 2 additionally runs the Judgehost service tests. Agent 1 runs package and
contest service tests. Agent 3 runs runtime startup E2E tests.

### Integrated checks

After all commits are integrated:

- run all four resource groups: `unit`, `service`, `executor`, and `e2e`;
- run all static checks and the resource manifest;
- run the project-owned deployed mock Judgehost E2E;
- inspect fresh and upgraded SQLite schemas;
- inspect one canonical ICPC ZIP plus two contest variants with different
  labels;
- run `git diff --check` and confirm the final worktree is clean.

Do not use a real Judgehost. Do not run tests on Windows. Do not push any branch
unless the user explicitly requests it.

## Out of scope

- Coalescing distinct export jobs.
- Changing `problem_package_builds` retry-in-place behavior.
- New package, schema, cache, or implementation version fields.
- Preserving old option-hash export cache rows.
- Full RuntimeConfig relocation.
- Judgehost wire, ACK, verdict, or cache identity changes.
- Artifact owner indexing (`STO-009`).
- Source-format canonicalization (`SRCFMT-001` and `SRCFMT-002`).
- Global authorization ownership or broad UI/test-suite cleanup.
