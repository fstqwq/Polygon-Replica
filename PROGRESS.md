# PROGRESS.md

This file tracks implementation status against `AGENTS.md` milestones.

## Current Status

- Overall: `Milestone 0` through `Milestone 7` implemented with a post-baseline optimization pass.
- Source of truth: Git history on `main`.
- Latest high-level completion commit before optimization pass: `ef3a51f`.
- Upstream asset integration commit: `6dc2851`.
- Latest tracker update commit before current working tree: `8225ed5`.

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
- Notes: Unified C++ compile path with cache key `(toolchain_digest, source_hash + testlib hash)`, include path for `third_party/testlib`, diagnostics parsing + file/line links.

5. Milestone 4: TeX Preview + Preview UI
- Status: Done
- Notes: TeX compile flow, `statement.pdf` output, `latex.log` capture, PDF preview and log references in UI, preview run status persisted.

6. Milestone 5: Build Pipeline + Build UI
- Status: Done
- Notes: compile -> generate -> validate -> solve pipeline, step logs, explicit failed step/test metadata, tests/ans browse/download paths.

7. Milestone 6: Runner + Run UI
- Status: Done
- Notes: pass-fail, interactive broker path, multi-pass with per-test feedback isolation, per-test/pass verdict and memory reporting, workspace or upload submission source.

8. Milestone 7: Exporters + Export UI
- Status: Done
- Notes: Kattis, DOMjudge legacy-icpc, Polygon standard/full zip generation with metadata in DB and download links in UI.

## Cross-Cutting Requirements

- Workspace-level mutation serialization:
  - Status: Done
  - Notes: lock file based workspace lock used for mutating operations.
- Always-visible workspace context in UI:
  - Status: Done
  - Notes: problem/workspace/branch/head/dirty/recent build and recent preview shown in shared header.
- Audit logging:
  - Status: Done
  - Notes: mutating actions write to `audit_log`.

## Optimization Pass (Latest)

- Added DB `previews` table and indexes for `workspaces/builds/previews/runs/exports/audit_log` hot paths.
- Added preview history API: `/api/problems/{problem}/workspaces/{user}/recent-previews`.
- Added recent runs/exports APIs: `/api/problems/{problem}/workspaces/{user}/recent-runs` and `/api/problems/{problem}/workspaces/{user}/recent-exports`.
- Added `recent_preview` field to workspace status API.
- Added global branch switching control in the shared header across all UI pages.
- Added artifact directory zip download endpoint and UI links (`tests.zip`, `ans.zip`, `feedback_dir.zip`).
- Build failure summaries now include `failed_step` and `failed_test`.
- Run execution now supports submission source from workspace path or direct file upload.
- Run compile diagnostics are parsed and surfaced in UI with file/line links when linkable.
- Added per-pass memory usage capture using `/usr/bin/time` when available (`0` fallback).
- Multi-pass feedback is isolated per test/pass to prevent cross-test contamination.
- Run detail now exposes concrete artifact links for interactive transcripts and key feedback files.
- Interactive runs now pass `FEEDBACK_DIR` to interactors for feedback file generation parity.
- Commit-based snapshot creation now uses `git archive` extraction (faster and avoids clone overhead).
- Build/solve/validate execution paths now use direct stdin/stdout process wiring (fewer shell invocations).
- Build config now supports explicit source overrides (`*_source`) and multi-generator lists (`generator_sources`, `generator_args`).
- Run submission compilation now reuses the unified toolchain compile cache.
- Ephemeral snapshot directories are cleaned after build/preview jobs.
- Preview service now fails gracefully when `pdflatex` is unavailable.
- Export generation hardened with metadata checks and guaranteed temp directory cleanup.
- Export generation now produces format-structured Kattis (`2025-09`) and DOMjudge legacy-icpc package layouts with statement/data/submissions/validators paths.
- Polygon exports are now slimmed to manifest + build step logs + statement preview (+ tests/ans only for full) and exclude heavy run replay payloads.
- Run submission flow now records deterministic failed run metadata (`summary.json`, `compile.log`) even when setup/compile throws before test execution.
- Build validator acceptance now supports both return code `0` and Kattis-style success code `42`.
- Runner checker/interactor verdict mapping now supports both testlib (`0`) and Kattis output-validator (`42=OK`, `43=WA`) conventions.
- Run submission source paths are now constrained to workspace boundaries to prevent path traversal.
- Build config now carries runner controls in `generation_params` (`checker_mode`, `checker_args`, `max_passes`, `validator_args`) for build-consistent run behavior.
- Runner now preflights selected `build_id` (existence/ownership/status) and records deterministic failed run metadata on invalid build selections.
- Runner preflight artifact existence check now evaluates pre-existing build artifact state before run directory creation (prevents false-positive runnable states).
- Runner checker execution now supports two invocation protocols:
  - `testlib`: `<checker> <input> <team_output> <answer>`
  - `kattis`: `<checker> <input> <answer> <feedback_dir>` with team output on stdin
- Toolchain compile cache keys are now dependency-aware for local quoted includes (`#include "..."`), preventing stale cache hits when header-only changes occur.
- Toolchain dependency cache keys now normalize dependency identities to source/include-relative paths (instead of absolute paths), restoring cache reuse across ephemeral snapshot roots.
- Build source discovery now resolves preferred C++ file stems across `.cpp/.cc/.cxx/.c++` variants.
- Runner fallback output checking now uses streaming file comparison (memory-safe for large outputs).
- Toolchain cache copy path now uses filesystem copy instead of in-memory byte duplication.
- Added reusable local validation script: `scripts/smoke_test.py`.
- Smoke coverage now includes `pass-fail`, `multi-pass`, `interactive`, missing-submission compile-failure handling, workspace path-traversal rejection, invalid-build preflight rejection, missing-artifacts preflight rejection, testlib checker mode, kattis checker mode, compile-cache reuse checks, compile-cache header-dependency invalidation, C++ `.cc` accepted-source builds, and export zip structure assertions.

## Upstream Dependency Integration

- testlib upstream: vendored under `third_party/upstream/testlib/`.
- Kattis package format upstream assets: vendored under `third_party/upstream/kattis/problem-package-format/`.
- Refresh script: `scripts/sync_upstream_assets.sh`.

## Validation Snapshot

- `python3 -m compileall app`: pass.
- Local smoke validation (venv, UI/API routing, build/preview/run/export path, upload run path): pass under local `./var` roots.
- `./scripts/smoke_test.py` (with `.venv/bin/python`, includes directory zip endpoint checks and export layout checks): pass.

## Known Operational Notes

- Host default roots (`/srv`, `/var/lib`, `/var/cache`) require appropriate filesystem permissions.
- For local development without elevated permissions, use env-mapped roots under `./var/`.
- Seeded repositories copy vendored `testlib.h` when available.
- TeX preview status is recorded even if LaTeX binaries are missing; preview compilation will be marked failed with a log entry.

## Update Policy

When significant work is merged, update this file with:
- milestone status changes,
- new known gaps or regressions,
- latest validation snapshot.
