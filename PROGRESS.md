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
- Run workspace submission paths now reject reserved internal workspace files (`.git/*`, `.polygonlike.lock`), symlinked submission aliases, and symlinked path components.
- Workspace file save/new/upload/download paths now reject symlinked path components to prevent alias-based path escapes.
- Git commit staging now explicitly excludes `.polygonlike.lock`, preventing internal workspace lock files from being committed into problem history.
- Git commit staging lock-file exclusion now also applies to nested `.polygonlike.lock` paths via glob pathspec filtering.
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
- Preview reuse candidate selection now scans paged history using keyset pagination (`created_at`,`id`) rather than offset paging, so poisoned/stale recent rows do not block valid cache hits and deep-history scans stay efficient.
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
- Export input-validator fallback now uses symlink-safe file detection and replaces symlinked `validator.cpp` paths before writing defaults, preventing symlink-target overwrite.
- Export test-data copy now streams safe test discovery and captures sample selection inline, avoiding full test-list materialization for large exports.
- Export test-data answer lookup now pre-indexes safe `ans/*.ans` files once per export, avoiding repeated per-test answer-path safety checks.
- Export test/answer top-level suffix discovery now uses deterministic `os.scandir`-based matching (`*.in`/`*.ans`), avoiding `glob` materialization while preserving symlink-safe ordering.
- Export build-root resolution now canonicalizes build artifact ids/roots and rejects traversal-style build ids before artifact reads.
- Kattis/DOMjudge export now rejects builds with missing `source_commit` metadata to preserve explicit commit provenance.
- Build/Preview/Run/Export pages and recent-* APIs are now workspace-scoped (`workspace_id` filtered or build-joined), preventing cross-user history leakage within the same problem.
- Export rows now persist `workspace_id` (with startup backfill for legacy rows), and Export page / recent-exports API now use direct workspace-scoped reads from `exports` without joining `builds`.
- Artifact endpoints (`browse`, `download-dir`, file reads) now verify artifact id ownership against the active workspace (builds and previews).
- Preview detail selection now validates `preview_id` ownership and suppresses foreign-workspace detail rendering.
- Run preflight now rejects cross-workspace build ids (`build does not belong to selected workspace`) with deterministic failed-run persistence.
- Export creation via UI now rejects build ids not owned by the active workspace.
- Build manifest API access is now workspace-scoped, and the legacy unscoped endpoint requires explicit workspace context.
- `page_ctx` now avoids redundant workspace-status refresh calls and supports branch-list skipping for endpoints that do not render branch controls.
- Workspace-context loading now supports optional recent-build/recent-preview suppression for non-render paths, reducing per-request metadata query load on mutation/recent-* routes and service entrypoints.
- `page_ctx` now skips unconditional `ensure_workspace` on non-refresh paths and lazily provisions only unknown-but-valid direct URL users, reducing steady-state provisioning/query overhead.
- Build/Run/Export UI queries now use narrow column projections for list/detail rows instead of `SELECT *`, reducing row payload size and DB-transfer overhead in hot views.
- Run/Export page build-selector queries are now capped to recent workspace builds (200 rows) to prevent unbounded dropdown payload growth on long-lived workspaces.
- Artifact and run-artifact browser pages now cap file listings (`512` entries) and surface truncation indicators, preventing oversized HTML responses on large artifact trees.
- Artifact and run-artifact browse list assembly now captures capped relative paths directly during traversal (instead of materializing `Path` objects then remapping), reducing temporary allocations on large artifact trees.
- Files page repository listing now caps rendered entries (`1024`) and surfaces truncation indicators, preventing oversized HTML responses on very large repositories.
- Files page file-content rendering now caps editor payloads (`131072` characters) and marks clipped views read-only with save disabled, preventing oversized editor responses and accidental truncation writes.
- Git page status/diff rendering now caps output (`512` status lines, `131072` diff characters) and surfaces truncation indicators, preventing oversized Git-page payloads on noisy workspaces.
- Git page diff collection now streams `git diff` output to temporary files and reads only a bounded prefix for rendering, preventing full in-memory diff buffering before truncation.
- Build/Preview log rendering now caps displayed log text (`131072` characters per log) and surfaces truncation indicators, preventing oversized page payloads from very large logs.
- Run detail rendering now caps displayed per-run test rows and compile diagnostics (`200` each) with truncation indicators, preventing oversized run-detail payloads on large runs.
- Run detail rendering now also caps per-test feedback-file links (`32` each) with truncation indicators, preventing pathological per-test link payload growth.
- Run persistence now caps `runs.summary_json` test/diagnostic/feedback lists at write time (with truncation metadata) while preserving full per-test results in run artifact `summary.json`, reducing DB metadata growth on large runs.
- Run-page list capping now preserves persisted run-summary truncation metadata when present, so UI indicators continue to reflect full-result totals from DB-capped summaries.
- Build detail rendering now caps displayed log-file entries and diagnostics (`200` each), and Preview now caps displayed log-reference entries (`200`), with truncation indicators to keep detail pages bounded on large metadata/log sets.
- Build persistence now caps `builds.summary_json` diagnostics at write time (with truncation metadata) while preserving full diagnostics in artifact `logs/diagnostics.json`, reducing DB metadata growth on diagnostic-heavy builds.
- Build/Run DB summary persistence now also truncates oversized diagnostic message text with message-level metadata, while full diagnostic text remains available in artifact logs/summaries.
- Build detail log-file discovery now uses bounded-memory selection (`scandir` + capped lexical selection) instead of materializing full sorted log lists, reducing memory pressure on very large artifact `logs/` directories.
- Build/Run detail summary parsing now caps `summary_json` UI decode input (`1048576` characters), returning a bounded fallback error for oversized payloads to avoid heavy decode/render paths from oversized DB blobs.
- Workspace manifest API parsing now caps `manifest.json` decode input (`2097152` characters), returning bounded fallback metadata for oversized manifests to avoid unbounded decode/response paths.
- Workspace manifest API now also caps large list fields (`files`: `512`, `steps`: `256`) with truncation metadata, preventing oversized manifest response payloads on large builds.
- Preview log-reference parsing now correctly matches standard `path.tex:line` entries in `latex.log`, restoring actionable file/line links in Preview.
- Workspace branch lists are now capped for UI/API rendering (`200` entries) with truncation indicators/metadata, preventing oversized header dropdown and branch API payloads on repos with many branches.
- UI/API branch-list retrieval now uses capped git ref queries (`for-each-ref --count limit+1`) on cache misses and a bounded TTL cache for capped results, avoiding repeated uncapped branch enumeration when only capped branch lists are required.
- `/api/problems` now uses a capped, narrow projection query (`200` rows) instead of unbounded `SELECT *`, preventing oversized problem-list payloads.
- Build/Run diagnostics now cap rendered diagnostic message text (`4096` characters per entry), preventing oversized diagnostic payloads from bloating detail pages.
- Workspace provisioning now supports optional status refresh while still forcing refresh on newly created workspace clones/rows.
- Workspace provisioning now has a steady-state fast path that bypasses provisioning-lock acquisition when workspace clone + DB row already exist, reducing lock contention on normal request paths.
- Workspace ensure/status-refresh flow now reuses previously resolved problem/user ids (and ensured user rows) instead of re-querying them during hot-path refresh updates.
- Workspace service now caches resolved `problems`/`users` rows in-process, reducing repeated metadata lookups during workspace ensure/context/status flows.
- Workspace problem/user metadata caches are now lock-protected and bounded with LRU-style eviction, preventing unbounded process-memory growth across large user/problem sets.
- Workspace status refresh now parses branch/dirty from one `git status --short --branch` invocation and only runs `rev-parse` for commit SHA, lowering git subprocess overhead.
- Workspace status refresh now prefers `git status --porcelain=2 --branch` to derive branch/head/dirty in one command, with fallback to legacy status parsing when needed.
- Workspace status refresh now conditionally updates workspace rows only when branch/head/dirty changed, reducing steady-state SQLite write churn during status polling.
- Workspace context now resolves latest build/preview metadata with one combined query, reducing DB round-trips on header/status render paths.
- Workspace artifact ownership checks now use id-prefix fast paths (`b-`/`p-`) with legacy fallback, reducing DB work on artifact browse/download/file requests.
- Build/Preview workspace-HEAD paths now use a read-only workspace status probe (no DB writes), reducing git/DB work while holding workspace locks.
- Preview creation now persists `artifact_path` at insert time, removing a redundant metadata update write per preview.
- Build/Preview snapshot creation now reuses already-known workspace HEAD/dirty state when available, avoiding duplicate `git status`/`rev-parse` subprocesses in hot mutation paths.
- Build finalization now updates `workspaces.recent_build_status` from in-process status tracking, removing a redundant post-build status lookup query.
- Build compile-stage logging now streams target entries directly to `logs/compile.log` during compile-result processing, avoiding in-memory accumulation of large compiler logs.
- Build compile-stage target logging now writes compiler stdout/stderr streams directly and parses diagnostics per stream, avoiding extra per-target merged-output string allocation while preserving empty-output diagnostic collection semantics.
- Build diagnostics parsing now resolves snapshot roots once per compiler-output chunk (instead of per matched diagnostic line), reducing repeated path-resolution overhead on noisy compile failures.
- Run submission compile logging now streams compiler stdout/stderr directly to run `compile.log` and parses diagnostics per stream, avoiding extra merged-log string allocation.
- Run compile diagnostics parsing now resolves workspace roots once per compiler-output chunk (instead of per matched diagnostic line), reducing repeated path-resolution overhead on noisy compile failures.
- Build validate/solve stages now stream per-test logs while collecting results, reducing peak memory usage on large test sets.
- Build generate stage now streams per-generator run entries directly into `generate.log`, avoiding in-memory accumulation on large generator batches.
- Build manual-test discovery now fast-paths `*.in` lookup before fallback to all files, reducing scan/memory overhead in `tests/manual` trees dominated by sidecar assets.
- Build manual-test discovery now drops fallback-file accumulation as soon as `*.in` tests are detected, reducing temporary memory usage in mixed manual-test directories while preserving deterministic no-`*.in` fallback behavior.
- Build manual-test discovery now uses a single symlink-safe traversal, avoiding duplicate scans and excluding symlinked manual test entries.
- Build manual-test discovery now classifies each directory pass before writing fallback entries, so directories containing safe `*.in` files avoid unnecessary fallback buffering and per-directory filename sorting.
- Build manual-test discovery directory pruning now sorts only kept subdirectories (instead of full `os.walk` directory lists), reducing traversal overhead on trees with many filtered entries.
- Build C++ source auto-discovery now performs a single deterministic directory pass with symlink-safe in-root filtering, reducing glob/sort overhead and preventing unsafe symlinked source selection.
- Toolchain dependency scanning now marks out-of-root quoted includes as cache-unsafe and bypasses compile-cache read/write for those compiles, preventing unsafe host-path dependency hashing and stale cache keys.
- Export source/mode detection now uses single-pass safe top-level file scanning, reducing repeated glob work and ignoring symlinked checker files during multi-pass mode inference.
- Export mode detection now scans checker sources in bounded chunks for `nextpass.in`, preserving multi-pass inference while avoiding full-file reads for large checker sources.
- Preview cache reuse now requires symlink-safe in-root regular files for `statement.pdf` and `latex.log`, preventing poisoned symlink preview artifacts from being reused.
- Preview page now validates `statement.pdf` and `latex.log` through safe artifact-path checks, so symlinked/out-of-root preview files are ignored in UI detail rendering.
- Workspace file listing now suppresses `.polygonlike.lock` entries in addition to `.git`, preventing lock-file metadata leaks into Files UI navigation.
- Preview compilation now persists source metadata (`source_commit`, `source_ref`) during finalization, removing redundant mid-run preview-row updates while preserving canonical commit/ref recording.
- Dirty workspace previews now clear `source_commit` metadata and skip commit-key reuse, preventing dirty snapshot outputs from polluting immutable commit preview cache provenance.
- Git page status flow now conditionally skips `git diff` unless porcelain status indicates unstaged tracked changes, reducing unnecessary subprocess work in clean/untracked/staged-only states.
- Git status filtering now drops internal lock-file entries (`.polygonlike.lock`) from rendered status/diff output, keeping Git UI output aligned with workspace dirty-state semantics.
- Git diff generation now applies lock-file exclusions directly in git pathspecs (with reserved-diff fallback filtering), reducing unnecessary diff parsing on lock-file-only changes.
- Artifact manifest generation now streams a deterministic sorted directory walk (symlink-skipping) instead of materializing `rglob` lists, lowering memory overhead on large artifact trees.
- Artifact manifest summary counters (`tests_count`, `ans_count`) are now computed during that same manifest walk, avoiding extra tests/ans directory scans.
- Added DB indexes for workspace-scoped history filters (`problem_id,workspace_id,created_at`) and preview-reuse lookup (`problem_id,source_commit,status,created_at`).
- Added direct `workspace_id` latest-row indexes for builds/previews to speed workspace-context `latest_*` lookups.
- Workspace branches API now falls back to the current branch on git branch-list failures (no 500 on transient git errors).
- Git branch enumeration now uses a short in-process cache with invalidation on branch switch/create/merge, reducing repeated branch-list subprocess calls on UI/API navigation paths.
- Git branch enumeration cache is now lock-protected and bounded with LRU-style eviction, preventing unbounded process-memory growth across many workspaces.
- Added workspace-scoped run-artifact endpoints (`/runs/{run_id}/artifacts/{rel}` and `/runs/{run_id}/download-dir`) and updated Run UI links to use run ownership instead of build-id path assumptions.
- Run artifact path resolution now supports both current run-root-relative paths and legacy `logs/run-<id>/...` paths for backward compatibility.
- Artifact directory browse/zip and run-artifact browse/zip now enforce symlink-safe file enumeration (no symlink-directory traversal, skip symlink entries, and skip out-of-root resolved targets).
- Artifact/run-artifact zip and export directory-copy flows now use iterator-based safe traversal streams instead of pre-materialized file lists (lower memory overhead for large trees).
- Run summaries now normalize `feedback_dir` to the stable relative token `feedback_dir` (avoids leaking host-absolute run paths in API/UI payloads).
- Run config loading now prefers a small `logs/run_config.json` sidecar (with manifest fallback), lazily backfills that sidecar for manifest-only legacy artifacts, and caches parsed settings in-process (copy-on-read), reducing repeated full-`manifest.json` reads/parses across submissions.
- Run test-input discovery now caches safe `.in` filename listings per build artifact root (copy-on-read), reducing repeated test-directory scans across submissions.
- Run test-input metadata (`name`,`stem`) is now cached per build artifact root (copy-on-read), reducing repeated stem parsing in run execution loops.
- Run answer-file discovery now caches safe `.ans` filename listings per build artifact root (copy-on-read), reducing repeated answer-directory scans across submissions.
- Run execution now reuses a cached immutable answer-name set per artifact root, avoiding repeated per-run list-to-set rematerialization for answer presence checks.
- Run artifact metadata caches now use bounded, thread-safe LRU-style retention, preventing unbounded process-memory growth across many distinct build artifacts.
- Run feedback key-file discovery (`judgemessage.txt`, `teammessage.txt`, `nextpass.in`) now validates feedback roots before traversal and uses a single symlink-safe directory walk per test, avoiding unsafe/out-of-root scans.
- Run feedback key-file discovery now also caps collected key files per test (`256`) to prevent pathological `feedback_files` list growth from deep feedback trees.
- Run feedback key-file discovery now sorts only matched key-file names per directory (instead of every filename), reducing scan overhead on large feedback trees while preserving deterministic ordering.
- Runner safe file matching now uses an `os.scandir` suffix fast path for common non-recursive patterns (`*.in`, `*.ans`), sorting only matched entries (instead of full-directory sorting), reducing run discovery overhead while preserving deterministic ordering and symlink safety.
- Runner safe file-matching now resolves artifact roots once per scan, reducing repeated path-resolution overhead during test/answer discovery.
- Switch workspace/branch routes now normalize posted page targets server-side (`artifacts`→`build`, `runs`→`run`, invalid→`files`) for redirect correctness without JS dependency.
- Build/Run detail pages now parse `summary_json` defensively and surface fallback errors for malformed JSON instead of raising 500 errors.
- Run artifact endpoints now validate filesystem-relative run-root shape (`<build>/logs/run-<run_id>` under problem artifacts, or `invalid-runs/<run_id>`) independent of DB-provided build-id strings, and reject DB-poisoned path overrides.
- Build detail log loading now uses canonical artifact roots (`artifacts/<problem>/<build_id>/logs`) instead of DB-provided build `artifact_path` metadata.
- Workspace manifest API now reads `manifest.json` through canonical safe artifact-path checks, rejecting symlinked/unsafe manifest targets.
- Artifact and run-artifact file/browse path resolution now rejects symlinked path components within artifact roots, preventing symlink-directory alias bypasses for direct file/directory views.
- Preview reuse now loads candidate artifacts from canonical roots (`artifacts/<problem>/<preview_id>`), rejects dotted/traversal-like preview ids, and ignores DB-provided preview `artifact_path` metadata.
- Build artifact path resolution now enforces canonical artifact-id roots, rejecting dotted/traversal-like build ids across browse/file/manifest paths.
- Canonical artifact-id validation is now shared (`[A-Za-z0-9_-]+`), and run/export preflight plus run-artifact root validation reject dotted ids in addition to traversal-style ids.
- Export preflight now validates required artifact path types/safety (`manifest.json` file, `logs/tests/ans` directories as required) and rejects malformed/symlinked required paths before packaging.
- Kattis/DOMjudge export source snapshotting now validates workspace metadata/path integrity (workspace row exists, DB path matches canonical `/workspaces/<uid>/<problem>`, `.git` present) and fails closed on mismatches.
- Workspace and provision lock acquisition now reject symlinked/invalid lock files (`O_NOFOLLOW` where available), preventing lock-file symlink redirection during mutation/provision flows.
- Files upload now uses same-directory temporary staging + atomic replace on success, preventing partial-file corruption while preserving chunked streaming.
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
- Smoke coverage now includes `pass-fail`, `multi-pass`, `interactive` (including interactive RE stderr transcript capture), snapshot clean/dirty path checks, snapshot symlink stripping checks, commit-ref canonicalization for build/preview source metadata, invalid commit-ref build/preview failure persistence, commit/workspace-head preview cache reuse checks (including deep poisoned-history scans and same-timestamp keyset pagination scans), missing-submission compile-failure handling, workspace path-traversal rejection, invalid-build preflight rejection, missing-artifacts/missing-tests-dir preflight rejection, invalid-run-mode preflight failure persistence (service + route paths), failed-build export rejection, missing-source-commit export rejection for Kattis/DOMjudge, repeated-export filename uniqueness, polygon-export snapshot independence, workspace-history isolation across pages/APIs, invalid problem/user identifier rejection in switch/status/build-run/preview-run/run-execute flows, cross-workspace artifact/preview/run/export rejection, workspace-scoped manifest API isolation, run-artifact endpoint coverage (including invalid-preflight run logs), build+preview artifact endpoint ownership checks, preview artifact-path persistence checks, symlink-safe artifact zip/browse hardening (file+directory symlink cases), symlink-safe workspace file listing checks, workspace lock-file listing suppression checks, workspace-head preview source-metadata persistence checks, dirty-workspace preview source-commit clearing checks, workspace DB-path integrity mismatch checks, concurrent workspace provisioning race checks, missing-workspace-row recovery checks, reserved workspace-path rejection for file CRUD/upload/download (`.git/*`, lock files), directory-target file-op rejection and missing-source rename handling, run-summary feedback path normalization, switch-page normalization robustness, malformed summary-json resilience checks, strict run-artifact root poisoning rejection checks (including traversal-style build-id poisoning), traversal-style build-id rejection in run/export service preflight, build-page artifact-path poisoning rejection checks, preview-reuse artifact-path/id poisoning rejection checks, preview-reuse symlinked-artifact rejection checks, dotted build-id artifact-root rejection checks (browse/manifest/detail), chunked file-upload binary round-trip checks, streamed run-upload route execution checks (including zero-byte uploads and empty-filename multipart fallbacks), Run/Export build-selector cap checks, artifact/run-artifact browse list-cap checks, files-page repository listing cap checks, Git tracked/untracked diff rendering checks, manifest ordering/self-exclusion checks, manifest summary tests/ans count checks, workspace status head/branch consistency checks, read-only workspace status helper checks, unchanged-workspace status-refresh write-elision checks, workspace status `recent_build`/`recent_preview` consistency checks, workspace `recent_build_status` consistency checks, non-UTF8 build/preview log rendering checks, files-page invalid-line query checks, binary non-UTF8 build/run round-trip checks, run input-artifact symlink hardening checks, run feedback-root symlink rejection checks, invalid-run path isolation, request-context refresh optimization safety, branch API resilience, Git branch-list cache invalidation checks, git branch-cache bound/eviction checks, workspace problem/user cache bound/eviction checks, build validate/solve per-test log entry checks, build generate log summary/per-generator entry checks, run config cache copy-safety checks, run test-input cache copy-safety checks, run test-input metadata cache copy-safety checks, run answer-file cache copy-safety checks, run artifact cache bound/eviction checks, run safe-matching suffix fast-path ordering/symlink checks, run feedback key-file discovery ordering/symlink checks, manual-test symlink filtering checks, build source auto-discovery symlink hardening checks, export validator fallback symlink-target protection checks, export low-sort symlink sample-selection checks, export sample/secret answer content checks, testlib checker mode, kattis checker mode, unchanged-build compile-cache reuse checks, compile-cache header-dependency invalidation, external-include compile-cache bypass checks, export mode-detection symlink hardening checks, compile-jobs/validate-jobs/solve-jobs/run-jobs/run-timeout propagation, deterministic non-interactive `TLE` verdict handling, effective validate/solve/run worker reporting (including multi-pass), manual-sidecar answer filtering, C++ `.cc` accepted-source builds, export source symlink hardening assertions, export build-artifact symlink hardening assertions, and export zip structure assertions.
- Smoke coverage now also validates dotted-id rejection in preview-reuse candidate scanning, run/export preflight build-id checks, and run-artifact root-shape checks.
- Smoke coverage now validates export preflight rejection when required artifact paths (`logs/`, `tests/`) are present as files instead of directories.
- Smoke coverage now validates export source-snapshot rejection for missing workspace metadata and DB workspace-path mismatches.
- Smoke coverage now validates symlinked workspace lock-path rejection and confirms blocked file writes remain unchanged.
- Smoke coverage now validates upload-route symlinked lock-path rejection (HTTP 400) and confirms no target file is created.
- Smoke coverage now validates Git page/backend status filtering for both root and nested lock-file workspace changes and confirms no lock-file diff output is produced.
- Smoke coverage now validates git commits never stage/commit root or nested `.polygonlike.lock` paths even when workspace locking is active.
- Smoke coverage now validates preview page rejection of symlinked preview `statement.pdf`/`latex.log` artifacts (no leak rendering / no PDF embed).
- Smoke coverage now validates workspace manifest endpoint rejection for symlinked `manifest.json` artifacts.
- Smoke coverage now validates artifact and run-artifact file/browse endpoint rejection for symlinked path-component aliases inside artifact roots.
- Smoke coverage now validates run submission-path rejection for reserved internal workspace files, symlinked submission aliases, and symlinked path components.
- Smoke coverage now validates Files-route rejection for symlinked workspace path components across save/new/upload/download operations.
- Smoke coverage now validates Git-page status-line and diff-character capping behavior, including truncation markers and UI indicators.
- Smoke coverage now validates Build/Preview page oversized-log truncation behavior, including UI indicators.
- Smoke coverage now validates Run-page `summary.tests` and `summary.compile_diagnostics` list capping behavior, including UI indicators.
- Smoke coverage now validates Build-page log-file/diagnostics list caps and Preview-page log-reference list caps, including UI indicators.
- Smoke coverage now validates Files-header branch-list caps and branches-API truncation metadata behavior.
- Smoke coverage now validates `/api/problems` problem-list cap behavior.
- Smoke coverage now validates Build/Run diagnostic-message truncation behavior with inline truncation markers.
- Smoke coverage now validates Files-page oversized file-content rendering caps, including read-only/disabled-save safeguards for truncated views.
- Smoke coverage now validates Build/Run page oversized `summary_json` rendering paths, ensuring bounded fallback errors instead of unbounded JSON decode in UI detail views.
- Smoke coverage now validates workspace-manifest API oversized `manifest.json` handling, ensuring bounded fallback metadata instead of unbounded JSON decode paths.
- Smoke coverage now validates capped-branch cache reuse behavior and bounded eviction for UI/API branch-list retrieval.
- Smoke coverage now validates run feedback key-file capped discovery behavior on large feedback trees.
- Smoke coverage now validates Run-page per-test feedback-file link capping behavior, including truncation indicators.
- Smoke coverage now validates run-config sidecar emission and run-config loading preference for `logs/run_config.json` over manifest generation-params.
- Smoke coverage now validates run-config sidecar backfill for manifest-only artifacts and sidecar reuse on subsequent config loads.
- Smoke coverage now validates workspace-manifest API files-list capping behavior with truncation metadata.
- Smoke coverage now validates `exports.workspace_id` persistence/index presence and workspace-scoped export history filtering behavior.
- Smoke coverage now validates export-mode chunked checker-marker scanning by detecting `nextpass.in` when split across chunk boundaries.
- Smoke coverage now validates DB-capped run summary persistence (tests truncation metadata) while run artifact `summary.json` retains full per-test results.
- Smoke coverage now validates Run-page indicator rendering for DB-capped run summaries using persisted truncation metadata.
- Smoke coverage now validates DB-capped build diagnostics persistence (with truncation metadata) while artifact `logs/diagnostics.json` retains full diagnostics.
- Smoke coverage now validates DB-persisted build/run oversized diagnostic-message truncation metadata, while artifact diagnostics preserve full message text.
- Smoke coverage now validates streamed build compile-log structure (`compile_jobs` header + expected target entries) for baseline build runs.
- Smoke coverage now validates streamed run compile logging by requiring non-empty run `compile.log` artifacts for failed uploaded-source compiles.
- Smoke coverage now validates run answer-file set caching parity with discovered answer names and enforces bounded eviction for that additional cache.
- Smoke coverage now validates export top-level suffix matching for deterministic safe `.ans` discovery with symlink skipping.
- Smoke coverage now validates build compile-stream helper empty-output behavior (single diagnostics collection with no emitted log text).
- Smoke coverage now validates manual-test source discovery preference (`*.in`) and deterministic no-`*.in` fallback behavior with symlink skipping.

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
