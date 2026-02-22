# PROGRESS.md

This file tracks implementation status against `AGENTS.md` milestones.

## Current Status

- Overall: `Milestone 0` through `Milestone 7` implemented in baseline form.
- Source of truth: Git history on `main`.
- Latest high-level completion commit before this tracker: `ef3a51f`.
- Upstream asset integration commit: `6dc2851`.

## Milestone Tracking

1. Milestone 0: Engineering Skeleton + UI Skeleton
- Status: Done
- Notes: FastAPI app, DB bootstrap, workspace context/status APIs, navigation shell in place.

2. Milestone 1: Git Workflow + Web Editing
- Status: Done
- Notes: Per-user workspace creation/clone, file CRUD/upload/download/rename, Git status/diff/commit/push/pull/switch/merge, UI pages wired.

3. Milestone 2: Artifacts Store + Build Records
- Status: Done
- Notes: Build-scoped artifact directory layout, DB build records, manifest writing, build detail/log visibility in UI.

4. Milestone 3: testlib.h + Unified Toolchain + Diagnostics
- Status: Done
- Notes: Unified C++ compile path with cache key `(toolchain_digest, source_hash)`, include path for `third_party/testlib`, diagnostics parsing + file/line links.

5. Milestone 4: TeX Preview + Preview UI
- Status: Done
- Notes: TeX compile flow, `statement.pdf` output, `latex.log` capture, PDF preview and log references in UI.

6. Milestone 5: Build Pipeline + Build UI
- Status: Done
- Notes: compile -> generate -> validate -> solve pipeline, step logs, failure surface, tests/ans browse/download paths.

7. Milestone 6: Runner + Run UI
- Status: Done
- Notes: pass-fail, interactive broker path, multi-pass loop using `feedback_dir/nextpass.in`, per-test/pass verdict reporting and run artifacts.

8. Milestone 7: Exporters + Export UI
- Status: Done
- Notes: Kattis, DOMjudge legacy-icpc, Polygon standard/full zip generation with metadata in DB and download links in UI.

## Cross-Cutting Requirements

- Workspace-level mutation serialization:
  - Status: Done
  - Notes: lock file based workspace lock used for mutating operations.
- Always-visible workspace context in UI:
  - Status: Done
  - Notes: problem/workspace/branch/head/dirty/recent build shown in shared header.
- Audit logging:
  - Status: Done
  - Notes: mutating actions write to `audit_log`.

## Upstream Dependency Integration

- testlib upstream: vendored under `third_party/upstream/testlib/`.
- Kattis package format upstream assets: vendored under `third_party/upstream/kattis/problem-package-format/`.
- Refresh script: `scripts/sync_upstream_assets.sh`.

## Validation Snapshot

- `python3 -m compileall app`: pass.
- Local smoke validation (venv, UI/API routing, build/run/export path): pass under local `./var` roots.

## Known Operational Notes

- Host default roots (`/srv`, `/var/lib`, `/var/cache`) require appropriate filesystem permissions.
- For local development without elevated permissions, use env-mapped roots under `./var/`.
- Seeded repositories now copy vendored `testlib.h` when available.

## Update Policy

When significant work is merged, update this file with:
- milestone status changes,
- new known gaps or regressions,
- latest validation snapshot.
