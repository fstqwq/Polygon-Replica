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
- Invalid preflight runs are now persisted under `run_root/invalid-runs` rather than creating synthetic build artifact directories.
- Runner preflight now canonicalizes build artifact ids/roots and rejects traversal-style build ids before artifact resolution.
- Runner checker execution now supports two invocation protocols:
  - `testlib`: `<checker> <input> <team_output> <answer>`
  - `kattis`: `<checker> <input> <answer> <feedback_dir>` with team output on stdin
- Toolchain compile cache keys are now dependency-aware for local quoted includes (`#include "..."`), preventing stale cache hits when header-only changes occur.
- Toolchain dependency cache keys now normalize dependency identities to source/include-relative paths (instead of absolute paths), restoring cache reuse across ephemeral snapshot roots.
- Build source discovery now resolves preferred C++ file stems across `.cpp/.cc/.cxx/.c++` variants.
- Build manual test ingestion now prefers `tests/manual/**/*.in` when present, avoiding accidental inclusion of sidecar `.ans` files as input tests.
- Build compile stage now supports bounded parallel target compilation (`compile_jobs`, with auto mode) and persists the effective value in manifest generation parameters.
- Build validate stage now supports bounded parallel validator execution (`validate_jobs`, with auto mode) and persists effective worker count in manifest generation parameters.
- Build solve stage now supports bounded parallel accepted-solution execution (`solve_jobs`, with auto mode) and persists effective worker count in manifest generation parameters.
- Runner non-interactive modes (`pass-fail`, `multi-pass`) now support bounded parallel per-test execution (`run_jobs`, with auto mode) sourced from build generation parameters.
- Runner now supports configurable per-test runtime limits (`run_timeout_sec`) sourced from build generation parameters.
- Run summaries now persist both configured and effective run-worker values (`run_jobs`, `run_jobs_effective`) for observability.
- Non-interactive runner timeout exceptions are now mapped to deterministic per-test `TLE` verdicts (instead of aborting the whole run).
- Toolchain compile cache population now uses per-key file locking with atomic replace to avoid duplicate/corrupted cache writes under concurrent compiles.
- Runner fallback output checking now uses streaming file comparison (memory-safe for large outputs).
- Export archive SHA-256 computation now streams file contents instead of reading full zip bytes into memory.
- Toolchain cache copy path now uses filesystem copy instead of in-memory byte duplication.
- Workspace snapshot creation now fast-paths clean workspaces with `git archive` and falls back to copy-tree for dirty workspaces to preserve uncommitted/untracked content.
- Workspace snapshots are now symlink-sanitized across both commit-archive and dirty-workspace capture paths to prevent symlink-based host file dereference in build/preview jobs.
- Commit snapshot extraction now uses a shell-free archive materialization path with safe tar-entry validation (eliminates `bash -lc` archive pipes).
- Preview service now reuses cached successful artifacts for identical commit-based preview requests (copying existing PDF/log).
- Preview service now also reuses cached artifacts for clean workspace-HEAD requests where source state is immutable.
- Preview reuse candidate selection now scans paged history (not a fixed small recent window), so poisoned/stale recent rows do not block valid cache hits.
- Build/Preview commit inputs are now canonicalized via `git rev-parse --verify <ref>^{commit}` so moving refs (e.g., `main`) map to immutable SHA metadata/cache keys.
- Build/Preview invalid commit refs are now captured as normal failed records (`status=failed`) with persisted error summaries/logs instead of uncaught service exceptions.
- Invalid commit-ref failures now retain the requested ref in `source_ref` metadata (instead of workspace branch fallback) for auditability.
- Interactive run IO broker now closes counterpart stdin on EOF, tolerates broken-pipe forwarding, and stores submission/interactor stderr in run artifacts (preventing common deadlock/failure modes).
- Runner build preflight now validates required artifact subdirectories (`tests/`, `ans/`) and persists detailed not-runnable reasons for incomplete artifact trees.
- Export preflight now enforces `build.status == ok`, validates required artifact paths per export type, and fails explicitly if source commit snapshot reconstruction fails.
- Export archives now use unique per-generation filenames (including `build_id` + `export_id`) to avoid overwriting prior exports and preserve DB row/file consistency.
- Export flow now snapshots source only for Kattis/DOMjudge; Polygon exports are sourced strictly from build artifacts and no longer depend on workspace/commit snapshot reconstruction.
- Kattis/DOMjudge export source snapshots are now symlink-sanitized before package assembly to prevent symlinked repository assets from leaking host files into exports.
- Export source snapshot extraction now resolves commit refs via `rev-parse --verify` and uses the shell-free safe archive extraction path.
- Export copy paths now enforce symlink-safe file enumeration for both source snapshots and build artifacts, preventing symlinked artifact inputs from leaking host files into package zips.
- Export build-root resolution now canonicalizes build artifact ids/roots and rejects traversal-style build ids before artifact reads.
- Kattis/DOMjudge export now rejects builds with missing `source_commit` metadata to preserve explicit commit provenance.
- Build/Preview/Run/Export pages and recent-* APIs are now workspace-scoped (`workspace_id` filtered or build-joined), preventing cross-user history leakage within the same problem.
- Artifact endpoints (`browse`, `download-dir`, file reads) now verify artifact id ownership against the active workspace (builds and previews).
- Preview detail selection now validates `preview_id` ownership and suppresses foreign-workspace detail rendering.
- Run preflight now rejects cross-workspace build ids (`build does not belong to selected workspace`) with deterministic failed-run persistence.
- Export creation via UI now rejects build ids not owned by the active workspace.
- Build manifest API access is now workspace-scoped, and the legacy unscoped endpoint requires explicit workspace context.
- `page_ctx` now avoids redundant workspace-status refresh calls and supports branch-list skipping for endpoints that do not render branch controls.
- Workspace-context loading now supports optional recent-build/recent-preview suppression for non-render paths, reducing per-request metadata query load on mutation/recent-* routes and service entrypoints.
- `page_ctx` now skips unconditional `ensure_workspace` on non-refresh paths and lazily provisions only unknown-but-valid direct URL users, reducing steady-state provisioning/query overhead.
- Build/Run/Export UI queries now use narrow column projections for list/detail rows instead of `SELECT *`, reducing row payload size and DB-transfer overhead in hot views.
- Workspace provisioning now supports optional status refresh while still forcing refresh on newly created workspace clones/rows.
- Added DB indexes for workspace-scoped history filters (`problem_id,workspace_id,created_at`) and preview-reuse lookup (`problem_id,source_commit,status,created_at`).
- Added direct `workspace_id` latest-row indexes for builds/previews to speed workspace-context `latest_*` lookups.
- Workspace branches API now falls back to the current branch on git branch-list failures (no 500 on transient git errors).
- Added workspace-scoped run-artifact endpoints (`/runs/{run_id}/artifacts/{rel}` and `/runs/{run_id}/download-dir`) and updated Run UI links to use run ownership instead of build-id path assumptions.
- Run artifact path resolution now supports both current run-root-relative paths and legacy `logs/run-<id>/...` paths for backward compatibility.
- Artifact directory browse/zip and run-artifact browse/zip now enforce symlink-safe file enumeration (no symlink-directory traversal, skip symlink entries, and skip out-of-root resolved targets).
- Artifact/run-artifact zip and export directory-copy flows now use iterator-based safe traversal streams instead of pre-materialized file lists (lower memory overhead for large trees).
- Run summaries now normalize `feedback_dir` to the stable relative token `feedback_dir` (avoids leaking host-absolute run paths in API/UI payloads).
- Switch workspace/branch routes now normalize posted page targets server-side (`artifacts`→`build`, `runs`→`run`, invalid→`files`) for redirect correctness without JS dependency.
- Build/Run detail pages now parse `summary_json` defensively and surface fallback errors for malformed JSON instead of raising 500 errors.
- Run artifact endpoints now validate filesystem-relative run-root shape (`<build>/logs/run-<run_id>` under problem artifacts, or `invalid-runs/<run_id>`) independent of DB-provided build-id strings, and reject DB-poisoned path overrides.
- Build detail log loading now uses canonical artifact roots (`artifacts/<problem>/<build_id>/logs`) instead of DB-provided build `artifact_path` metadata.
- Preview reuse now loads candidate artifacts from canonical roots (`artifacts/<problem>/<preview_id>`), rejects dotted/traversal-like preview ids, and ignores DB-provided preview `artifact_path` metadata.
- Build artifact path resolution now enforces canonical artifact-id roots, rejecting dotted/traversal-like build ids across browse/file/manifest paths.
- Workspace file listing now performs symlink-safe traversal (no symlink-directory walk; symlinked outside files/dirs are excluded from Files page listing).
- Workspace context now validates DB workspace-path integrity against expected per-user location and rejects mismatched/missing/non-git workspace paths.
- Workspace context integrity failures now propagate as deterministic HTTP 500 responses instead of uncaught runtime exceptions in request handlers.
- Workspace provisioning now serializes first-time clone/DB-row creation per user+problem (lock-file guarded) to avoid concurrent provisioning races.
- Problem/user identifiers are now validated for provisioning and workspace-status paths to reject unsafe slugs before filesystem/DB lookup.
- Build/Preview/Run mutation routes now surface invalid problem/user identifiers as HTTP 400 responses (not uncaught service-layer ValueError traces).
- Files CRUD/upload/download now reject reserved workspace-internal paths (for example `.git/*` and `.polygonlike.lock`) and avoid 500s on these invalid operations.
- Files upload/save/new now reject directory-target paths, and rename now reports missing-source errors deterministically instead of surfacing raw filesystem exceptions.
- Files upload endpoint now streams payloads to disk in chunks (memory-safe for large uploads).
- Run execution upload flow now streams uploaded submissions directly into run artifacts (memory-safe for large uploads from the Run page).
- Run submission upload handling now treats zero-byte uploads as explicit compile inputs (failing with compile diagnostics instead of `submission_path`-required errors).
- Run execute route now ignores empty upload placeholders (`filename=""`) so multipart forms keep using `submission_path` when no file is selected.
- Unsupported run modes now persist deterministic failed run metadata during preflight (instead of surfacing uncaught route/service exceptions).
- Build/Preview log rendering now decodes text with UTF-8 replacement semantics (no 500 on non-UTF8 log bytes).
- Files page now sanitizes `line` query parsing and defaults malformed/non-positive values to keep rendering stable.
- Build generator execution now streams stdout directly into test files (memory-safe for large generated tests).
- Process execution now uses binary-safe stdin/stdout redirection for file-backed IO paths, preventing non-UTF8 test data corruption in build/run stages.
- Run execution now ignores symlinked test inputs and rejects invalid/symlinked checker/interactor/answer artifact paths to prevent out-of-root artifact dereference during judging.
- Build generation logs now report `manual_tests`, `generated_tests`, and `total_tests` counters explicitly.
- Added reusable local validation script: `scripts/smoke_test.py`.
- Smoke coverage now includes `pass-fail`, `multi-pass`, `interactive` (including interactive RE stderr transcript capture), snapshot clean/dirty path checks, snapshot symlink stripping checks, commit-ref canonicalization for build/preview source metadata, invalid commit-ref build/preview failure persistence, commit/workspace-head preview cache reuse checks (including deep poisoned-history scan), missing-submission compile-failure handling, workspace path-traversal rejection, invalid-build preflight rejection, missing-artifacts/missing-tests-dir preflight rejection, invalid-run-mode preflight failure persistence (service + route paths), failed-build export rejection, missing-source-commit export rejection for Kattis/DOMjudge, repeated-export filename uniqueness, polygon-export snapshot independence, workspace-history isolation across pages/APIs, invalid problem/user identifier rejection in switch/status/build-run/preview-run/run-execute flows, cross-workspace artifact/preview/run/export rejection, workspace-scoped manifest API isolation, run-artifact endpoint coverage (including invalid-preflight run logs), symlink-safe artifact zip/browse hardening (file+directory symlink cases), symlink-safe workspace file listing checks, workspace DB-path integrity mismatch checks, concurrent workspace provisioning race checks, reserved workspace-path rejection for file CRUD/upload/download (`.git/*`, lock files), directory-target file-op rejection and missing-source rename handling, run-summary feedback path normalization, switch-page normalization robustness, malformed summary-json resilience checks, strict run-artifact root poisoning rejection checks (including traversal-style build-id poisoning), traversal-style build-id rejection in run/export service preflight, build-page artifact-path poisoning rejection checks, preview-reuse artifact-path/id poisoning rejection checks, dotted build-id artifact-root rejection checks (browse/manifest/detail), chunked file-upload binary round-trip checks, streamed run-upload route execution checks (including zero-byte uploads and empty-filename multipart fallbacks), non-UTF8 build/preview log rendering checks, files-page invalid-line query checks, binary non-UTF8 build/run round-trip checks, run input-artifact symlink hardening checks, invalid-run path isolation, request-context refresh optimization safety, branch API resilience, testlib checker mode, kattis checker mode, unchanged-build compile-cache reuse checks, compile-cache header-dependency invalidation, compile-jobs/validate-jobs/solve-jobs/run-jobs/run-timeout propagation, deterministic non-interactive `TLE` verdict handling, effective validate/solve/run worker reporting (including multi-pass), manual-sidecar answer filtering, C++ `.cc` accepted-source builds, export source symlink hardening assertions, export build-artifact symlink hardening assertions, and export zip structure assertions.

## Upstream Dependency Integration

- testlib upstream: vendored under `third_party/upstream/testlib/`.
- Kattis package format upstream assets: vendored under `third_party/upstream/kattis/problem-package-format/`.
- Refresh script: `scripts/sync_upstream_assets.sh`.

## Validation Snapshot

- `python3 -m compileall app scripts/smoke_test.py`: pass.
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
