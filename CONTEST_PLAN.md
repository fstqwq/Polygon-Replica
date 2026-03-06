# CONTEST_PLAN

Last updated: 2026-03-05

## Fixed product rules

1. Contest does not pin problem revision.
- Keep unique constraints:
  - `(contest_id, problem_id)`
  - `(contest_id, idx)`
- Contest always uses latest committed state of each problem.

2. `Change names and TL/ML` is a source-changing action.
- It must write problem source files.
- It must create per-problem commits.
- It must report per-problem result (`ok`/`failed`, commit id, error).

## Delivery phases

### Phase 1: Core contest authoring
- Contest pages: `overview`, `problems`, `properties`, `access`, `packages`.
- Problem add/remove/reorder/renumber workflow.
- ACL and properties editing.

### Phase 2: Batch edits and review
- Batch `Change names and TL/ML` UI.
- Per-problem workspace lock + commit.
- Readable batch execution report and retry.
- Contest statement preview with compile diagnostics.

### Phase 3: Package pipeline
- Async contest package build/export.
- Job timeline/logs/artifacts in UI.
- ICPC package output checks.

## Acceptance

1. Create contest and manage problem list/indexes.
2. Run batch rename/TL/ML and see commits for each changed problem.
3. Preview statements and receive actionable compile errors.
4. Build/export contest package with visible async status and downloadable artifacts.
