# Polygonlike Authoring System

This repository implements a local Polygon-like problem authoring system aligned with `AGENTS.md`:

- Git-backed per-problem repositories with per-user workspaces
- Local filesystem artifact store (`tests`, `ans`, `logs`, `statement_preview`, `export`, `manifest.json`)
- Minimum metadata database schema (`problems`, `users`, `repo_acl`, `workspaces`, `builds`, `previews`, `runs`, `exports`, `audit_log`)
- Unified compiler layer with cache key `(toolchain_digest, source_hash)` and `testlib.h` include path support
- TeX preview compilation and log capture
- Build pipeline (`compile -> generate -> validate -> solve -> persist`) with failed-step metadata
- Runner page with pass-fail / interactive / multi-pass modes and workspace-or-upload submissions
- Exporter page for Kattis / DOMjudge / Polygon zips
  - Kattis and DOMjudge exports now emit format-structured package layouts (problem metadata, statement, test data, submissions, validators)
  - Polygon exports are slimmed to build outputs and step logs (run replay payloads excluded)
- Artifact browsing plus directory zip download endpoints for generated outputs
- Build config supports explicit source overrides and multi-generator inputs (`config/build.json`)
- Build config also supports runner-facing controls:
  - `validator_args`
  - `checker_mode` (`testlib` or `kattis`)
  - `checker_args`
  - `max_passes`
  - `run_timeout_sec`
- Build source discovery now supports C++ variants (`.cpp`, `.cc`, `.cxx`, `.c++`) with preferred-name resolution (`accepted.*`, etc.)
- Manual test ingestion now prefers `tests/manual/**/*.in` when present, so sidecar files like `.ans` are not treated as input tests
- Build compile step now supports bounded parallel target compilation via `config/build.json` `compile_jobs` (`0` = auto)
- Build validate step now supports bounded parallel validator execution via `config/build.json` `validate_jobs` (`0` = auto)
- Build solve step now supports bounded parallel accepted-solution execution via `config/build.json` `solve_jobs` (`0` = auto)
- Runner non-interactive execution (`pass-fail`, `multi-pass`) now supports bounded parallel per-test execution via `config/build.json` `run_jobs` (`0` = auto)
- Web UI sections: Files, Git, Build, Preview, Run, Export
- Workspace-level mutation locking and audit log entries
- Run failure hardening: compilation/setup errors now always finalize run status with `summary.json` and `compile.log`
- Validator/checker/interactor compatibility: accepts both testlib-style (`0`) and Kattis-style (`42/43`) verdict exit codes
- Run source safety: workspace submission paths are validated to stay within workspace root
- Run preflight hardening: non-existent/non-ready build ids are rejected as failed runs with persisted logs/summary
- Run preflight now also rejects builds whose artifact directories are missing/corrupted before execution starts
- Run preflight now canonicalizes build artifact ids/roots and rejects traversal-style build ids before artifact resolution.
- Run execution now ignores symlinked test inputs and rejects invalid/symlinked checker/interactor/answer artifact paths to prevent out-of-root artifact dereference during judging.
- Invalid preflight runs are isolated under `run_root/invalid-runs/` rather than creating synthetic build artifact trees
- Compile cache correctness: cache keys now include recursively discovered local `#include "..."` dependencies (source dir + include dirs), so header-only changes invalidate stale binaries
- Compile cache keys now use canonical dependency identities (relative to source/include roots) rather than absolute filesystem paths, preserving cache reuse across snapshot directories
- Compile cache writes are now serialized per cache key with file locking and atomic replace, preventing duplicate/corrupted cache artifacts under concurrent compilation
- Run summaries now include both configured and effective run worker counts (`run_jobs`, `run_jobs_effective`)
- Runner now supports configurable per-test runtime limits from build config (`run_timeout_sec`) and records it in run/build metadata.
- Non-interactive runner timeouts now produce deterministic per-test `TLE` verdicts instead of aborting whole runs on subprocess timeout exceptions.
- Run fallback judging (when no checker binary is available) now performs chunked file comparison to avoid loading full outputs into memory
- Export zip hashing now streams file contents (no full-archive memory read during digest computation)
- Workspace snapshot creation now fast-paths clean workspaces via `git archive` and falls back to full copy for dirty workspaces to preserve uncommitted/untracked files
- Snapshot materialization now strips symlink entries (for both commit-archive and dirty-workspace paths) to prevent symlink-based host file dereference during build/preview execution.
- Commit snapshot extraction now uses shell-free `git archive` materialization with safe tar-entry validation (no `bash -lc` pipeline).
- Preview compilation now reuses cached successful artifacts for identical commit-based preview requests (copying cached PDF/log instead of re-running TeX)
- Preview compilation now also reuses cached artifacts for clean workspace-HEAD requests when immutable.
- Preview reuse candidate selection now scans paged history with keyset pagination (`created_at`,`id`) instead of offset paging, preventing stale/poisoned recent rows from starving valid cache hits while keeping deep-history scans efficient.
- Build/Preview commit selectors now canonicalize refs (`main`, tags, short SHAs) to immutable commit SHAs before snapshot/cache decisions.
- Build/Preview invalid commit refs are now persisted as failed records (with error summaries) instead of uncaught service exceptions.
- Failed commit-ref jobs now preserve the originally requested ref in `source_ref` metadata for clearer audit/provenance.
- Interactive runner IO broker now captures submission/interactor stderr into run artifacts and hardens pipe forwarding against early-exit/broken-pipe deadlocks.
- Runner preflight now validates required build artifact directories (`tests/`, `ans/`) and returns explicit not-runnable reasons with deterministic invalid-run isolation.
- Export generation now enforces successful build status and required artifact presence, and fails explicitly when commit snapshot reconstruction is not possible.
- Export generation now canonicalizes build artifact ids/roots and rejects traversal-style build ids before reading artifact trees.
- Kattis/DOMjudge export now requires non-empty build `source_commit` metadata; missing commit provenance is rejected explicitly.
- Export generation now writes unique per-generation zip filenames (type + build + export id) so repeated exports do not overwrite historical packages.
- Polygon exports now operate from build artifacts only (no source snapshot dependency), while Kattis/DOMjudge keep strict commit-snapshot enforcement.
- Kattis/DOMjudge source snapshots are now symlink-sanitized before export assembly, preventing symlinked repository assets from being copied from host paths.
- Kattis/DOMjudge export source snapshots now resolve commit refs with `rev-parse --verify` before extraction, and use the same shell-free safe archive extraction path.
- Export copy paths now use symlink-safe file enumeration for both source snapshots and build artifacts, preventing symlinked build inputs from leaking host files into generated archives.
- Workspace pages and recent-* APIs are now strictly workspace-scoped (build/preview/run/export history no longer leaks entries from other users on the same problem).
- Artifact download/browse/file endpoints now enforce workspace ownership for both build and preview artifact ids.
- Preview page now ignores foreign-workspace `preview_id` query values to prevent cross-workspace log/PDF detail leaks.
- Export UI now rejects cross-workspace `build_id` selections before export generation.
- Runner preflight now rejects cross-workspace `build_id` selections with deterministic failed-run metadata.
- Manifest API access is now workspace-scoped (`/api/problems/{problem}/workspaces/{user}/builds/{build_id}/manifest`), and the legacy unscoped route requires explicit workspace context.
- Request-context refresh optimization: `page_ctx` no longer performs redundant workspace status refreshes, and non-UI API/file routes skip branch-list resolution.
- Workspace context loading now supports optional recent-build/recent-preview skipping, and non-render mutation/API/service paths use this lightweight mode to reduce hot-path DB queries.
- `page_ctx` now avoids unconditional workspace provisioning on non-refresh requests, with lazy fallback provisioning only for unknown-but-valid users opened directly via URL.
- Build/Run/Export page queries now project only template-required columns for list/detail rows (instead of broad `SELECT *` payloads), reducing DB row transfer on hot UI views.
- Run/Export build-selector queries are now bounded to recent workspace builds (200 rows), preventing unbounded dropdown payload growth on long-lived workspaces.
- Workspace bootstrap now supports optional status refresh with safe auto-refresh on newly created workspaces.
- Workspace provisioning now has a steady-state fast path that skips provisioning-lock acquisition for already-provisioned workspaces, reducing lock contention on normal page/API traffic.
- Workspace provisioning/status refresh now reuses already-resolved problem/user ids in `ensure_workspace` paths (and returns ensured user rows), reducing redundant metadata queries on hot request flows.
- Workspace service now caches resolved problem/user metadata rows in-process to cut repeated DB lookups across `ensure_workspace`, context, and status-refresh hot paths.
- Workspace status refresh now derives branch + dirty state from a single `git status --short --branch` call (plus `rev-parse` for commit SHA), reducing per-refresh git subprocess count.
- Workspace status refresh now prefers `git status --porcelain=2 --branch` to derive branch/head/dirty in one command (with fallback to legacy parsing), further reducing git subprocess overhead.
- Workspace status refresh now conditionally updates workspace metadata only when branch/head/dirty changed, reducing steady-state SQLite write churn on read-heavy UI/API polling.
- Workspace context now fetches latest build/preview metadata in one combined query, reducing DB round-trips for header/status rendering paths.
- Workspace artifact ownership checks now use id-prefix fast paths (`b-`/`p-`) with legacy fallback, reducing DB work on artifact browse/download/file endpoints.
- Build/Preview workspace-HEAD flows now use a read-only workspace status probe (no DB writes), reducing git/DB work under workspace locks.
- Preview creation now writes `artifact_path` at row insert time, removing a redundant follow-up preview metadata update.
- Build/Preview snapshot creation now reuses already-known workspace HEAD/dirty state when available, avoiding duplicate `git status`/`rev-parse` subprocesses on hot build/preview paths.
- Build finalization now updates `workspaces.recent_build_status` from in-process status tracking, avoiding a redundant post-build `SELECT status FROM builds` query.
- Build validate/solve stages now stream per-test logs while collecting results, reducing peak memory usage on large test sets.
- Build manual-test discovery now fast-paths `*.in` lookup before fallback to all files, reducing scan/memory overhead in `tests/manual` trees dominated by sidecar assets.
- Git page status now runs `git diff` only when unstaged tracked changes are present; clean, staged-only, and untracked-only states skip the extra diff subprocess.
- Artifact manifest writing now uses a single deterministic streaming directory walk (instead of materializing full `rglob` lists), reducing memory overhead for large build artifacts.
- Artifact manifest summary counters (`tests_count`, `ans_count`) are now computed during that same manifest walk, avoiding extra tests/ans directory scans.
- Added DB indexes for workspace-scoped history queries and preview reuse lookup hot paths.
- Added direct `workspace_id` latest-row indexes for builds/previews to accelerate workspace header status queries.
- Branch-list API now degrades safely to current branch on git enumeration errors instead of returning 500.
- Git branch enumeration now uses a short in-process cache with invalidation on branch switch/create/merge, reducing repeated branch-list subprocess calls on UI/API navigation paths.
- Run detail links now use workspace-scoped run-artifact endpoints (`/runs/{run_id}/artifacts/...` and `/runs/{run_id}/download-dir`) so replay files work for both valid builds and invalid preflight runs.
- Artifact and run-artifact browse/zip directory exports now perform symlink-safe walks (no symlink-directory traversal) and skip out-of-root resolved targets to prevent archive-path escape/exfiltration via crafted artifact trees.
- Artifact/run-artifact zip generation and export directory-copy walks now stream from iterator-based safe traversal (reducing peak memory on large artifact trees).
- Run summaries now expose `feedback_dir` as a stable run-relative path (`feedback_dir`) instead of host-absolute filesystem paths.
- Run config loading now caches parsed per-build manifest runner settings in-process (copy-on-read), reducing repeated `manifest.json` reads/parses across submissions.
- Run test-input discovery now caches safe `.in` filename listings per build artifact root (copy-on-read), reducing repeated test-directory scans across submissions.
- Run test-input metadata (`name`,`stem`) is now cached per build artifact root (copy-on-read), reducing repeated stem parsing in run execution loops.
- Run answer-file discovery now caches safe `.ans` filename listings per build artifact root (copy-on-read), reducing repeated answer-directory scans across submissions.
- Runner safe file-matching now resolves artifact roots once per scan, reducing repeated path-resolution overhead during test/answer discovery.
- Workspace/branch switch routes now normalize page targets server-side (`artifacts`→`build`, `runs`→`run`, invalid→`files`), so redirects remain valid even without client-side page-target JS.
- Build/Run detail pages now handle malformed `summary_json` rows defensively (no 500 on corrupted metadata; fallback error shown in UI).
- Run artifact endpoints now validate filesystem-relative run-root shape (`<build>/logs/run-<run_id>` under problem artifacts, or `invalid-runs/<run_id>`) independent of DB-provided build-id strings, and reject DB-poisoned path overrides.
- Build detail log loading now uses canonical artifact roots (`artifacts/<problem>/<build_id>/logs`) instead of DB-provided `artifact_path` values.
- Preview reuse now loads candidate artifacts from canonical roots (`artifacts/<problem>/<preview_id>`), rejects dotted/traversal-like preview ids, and ignores DB-provided preview `artifact_path` values.
- Build artifact paths now enforce canonical artifact-id roots, rejecting dotted/traversal-like build ids across artifact browse/file/manifest flows.
- Workspace file listing now performs symlink-safe traversal (no symlink-directory walk; symlinked outside files/dirs are excluded from Files page listing).
- Workspace context now validates DB workspace-path integrity against expected per-user location and rejects mismatched/missing/non-git workspaces.
- Workspace context integrity failures are now surfaced as deterministic HTTP 500 responses (not uncaught server exceptions).
- Workspace provisioning is now protected by a per-user/problem lock to avoid clone/DB-row races during concurrent first-time workspace creation.
- Problem/user identifiers are now validated before provisioning/status queries to prevent unsafe slug inputs from escaping managed workspace/repo roots.
- Build/Preview/Run mutate routes now map invalid problem/user identifiers to HTTP 400 instead of uncaught service exceptions.
- Files operations now reject reserved workspace-internal paths (for example `.git/*` and `.polygonlike.lock`) to prevent UI edits/downloads of git metadata.
- Files upload/save/new/rename paths now reject directory-target writes and missing-source rename cases with deterministic user-facing errors (no 500s).
- Files upload endpoint now streams uploads to disk in chunks to avoid buffering whole files in memory.
- Run execution upload flow now streams uploaded submissions directly to run artifacts instead of buffering entire files in request memory.
- Run execution now treats zero-byte uploads as explicit submissions (compile-error outcomes) instead of falling back to `submission_path` validation errors.
- Run execute route now ignores empty upload placeholders (`filename=""`) so multipart form submits still use `submission_path` when no file is selected.
- Invalid run modes now produce deterministic failed run records with persisted preflight reasons (instead of surfacing uncaught route errors).
- Build and Preview pages now decode logs with UTF-8 replacement semantics, avoiding 500s on non-UTF8 tool output.
- Files page now sanitizes `line` query parsing and defaults invalid/non-positive values instead of raising 500.
- Build generator execution now streams output directly into test files to avoid buffering full generated tests in memory
- Command execution file redirection now uses binary-safe stdin/stdout piping, preventing non-UTF8 test data corruption in build/run flows.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./scripts/bootstrap_demo.sh
```

Open: `http://127.0.0.1:8000`

## Notes

- Default host roots follow `AGENTS.md` (`/srv/git`, `/srv/workspaces`, `/srv/runs`, `/var/lib/polygonlike/artifacts`, `/var/cache/polygonlike`).
- For local dev without root paths, `scripts/bootstrap_demo.sh` maps all roots under `./var/`.
- Build diagnostics are parsed and linked to Files editor paths and lines.
- Upstream assets are vendored under `third_party/upstream/`:
  - `testlib.h` from `MikeMirzayanov/testlib`
  - Kattis package spec/schemas/examples from `Kattis/problem-package-format`
- Refresh vendored upstream files with:
  - `./scripts/sync_upstream_assets.sh`
- Run local end-to-end validation with:
  - `.venv/bin/python ./scripts/smoke_test.py`
  - Covers pass-fail, multi-pass, and interactive run flows (including interactive RE stderr transcript capture), snapshot clean/dirty path behavior, snapshot symlink stripping checks, commit-ref canonicalization, invalid-commit build/preview failure persistence, commit/workspace-head preview artifact reuse (including deep poisoned-history scans and same-timestamp keyset pagination scans), `compile_jobs`/`validate_jobs`/`solve_jobs`/`run_jobs` propagation, `run_timeout_sec` propagation with non-interactive `TLE` verdict handling, validate/solve/run worker effectiveness reporting, compile cache reuse/invalidation checks, manual sidecar answer-file filtering, missing-submission failure handling, invalid-build/missing-artifacts/missing-tests-dir preflight handling, invalid-run-mode preflight failure persistence (service + route paths), failed-build export rejection, missing-source-commit export rejection for Kattis/DOMjudge, repeated-export filename uniqueness, polygon-export snapshot independence, workspace-history isolation across pages/APIs, workspace path-boundary rejection, invalid problem/user identifier rejection in switch/status/build-run/preview-run/run-execute flows, cross-workspace artifact/preview/run/export rejection, workspace-scoped manifest API isolation, run-artifact endpoint coverage (including invalid-preflight runs), build+preview artifact endpoint ownership checks, preview artifact-path persistence checks, symlink-safe artifact zip/browse hardening, symlink-safe workspace file listing checks, workspace DB-path integrity mismatch checks, concurrent workspace provisioning race checks, missing-workspace-row recovery checks, reserved workspace-path rejection for file CRUD/upload/download (`.git/*`, lock files), directory-target file-op rejection and missing-source rename handling, run-summary feedback path normalization, switch-page normalization robustness, malformed summary-json resilience on build/run pages, strict run-artifact root poisoning rejection checks (including traversal-style build-id poisoning), traversal-style build-id rejection in run/export service preflight, build-page artifact-path poisoning rejection checks, preview-reuse artifact-path/id poisoning rejection checks, dotted build-id artifact-root rejection checks (browse/manifest/detail), chunked file-upload binary round-trip checks, streamed run-upload route execution checks (including zero-byte uploads and empty-filename multipart fallbacks), Run/Export build-selector list-cap checks, Git tracked/untracked diff rendering checks, manifest ordering/self-exclusion checks, manifest summary tests/ans count checks, workspace status head/branch consistency checks, read-only workspace status helper checks, unchanged-workspace status-refresh write-elision checks, workspace status `recent_build`/`recent_preview` consistency checks, workspace `recent_build_status` update checks, non-UTF8 build/preview log rendering resilience, files-page invalid-line query resilience, binary non-UTF8 build/run test-data round-trip checks, run input-artifact symlink hardening checks, invalid-run path isolation, request-context refresh optimization safety, branch API resilience, Git branch-list cache invalidation checks, build validate/solve per-test log entry checks, run config cache copy-safety checks, run test-input cache copy-safety checks, run test-input metadata cache copy-safety checks, run answer-file cache copy-safety checks, testlib checker mode, kattis checker mode, unchanged-build compile-cache reuse checks, compile-cache header-dependency invalidation, compile-jobs/validate-jobs/solve-jobs/run-jobs/run-timeout propagation, deterministic non-interactive `TLE` verdict handling, effective validate/solve/run worker reporting (including multi-pass), manual-sidecar answer filtering, C++ `.cc` accepted-source builds, export source symlink hardening assertions, export build-artifact symlink hardening assertions, and export zip structure checks.
