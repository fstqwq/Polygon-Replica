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
- Invalid preflight runs are isolated under `run_root/invalid-runs/` rather than creating synthetic build artifact trees
- Compile cache correctness: cache keys now include recursively discovered local `#include "..."` dependencies (source dir + include dirs), so header-only changes invalidate stale binaries
- Compile cache keys now use canonical dependency identities (relative to source/include roots) rather than absolute filesystem paths, preserving cache reuse across snapshot directories
- Compile cache writes are now serialized per cache key with file locking and atomic replace, preventing duplicate/corrupted cache artifacts under concurrent compilation
- Run summaries now include both configured and effective run worker counts (`run_jobs`, `run_jobs_effective`)
- Run fallback judging (when no checker binary is available) now performs chunked file comparison to avoid loading full outputs into memory
- Export zip hashing now streams file contents (no full-archive memory read during digest computation)
- Workspace snapshot creation now fast-paths clean workspaces via `git archive` and falls back to full copy for dirty workspaces to preserve uncommitted/untracked files
- Preview compilation now reuses cached successful artifacts for identical commit-based preview requests (copying cached PDF/log instead of re-running TeX)
- Preview compilation now also reuses cached artifacts for clean workspace-HEAD requests when immutable.
- Build/Preview commit selectors now canonicalize refs (`main`, tags, short SHAs) to immutable commit SHAs before snapshot/cache decisions.
- Build/Preview invalid commit refs are now persisted as failed records (with error summaries) instead of uncaught service exceptions.
- Failed commit-ref jobs now preserve the originally requested ref in `source_ref` metadata for clearer audit/provenance.
- Interactive runner IO broker now captures submission/interactor stderr into run artifacts and hardens pipe forwarding against early-exit/broken-pipe deadlocks.
- Runner preflight now validates required build artifact directories (`tests/`, `ans/`) and returns explicit not-runnable reasons with deterministic invalid-run isolation.
- Export generation now enforces successful build status and required artifact presence, and fails explicitly when commit snapshot reconstruction is not possible.
- Export generation now writes unique per-generation zip filenames (type + build + export id) so repeated exports do not overwrite historical packages.
- Polygon exports now operate from build artifacts only (no source snapshot dependency), while Kattis/DOMjudge keep strict commit-snapshot enforcement.
- Workspace pages and recent-* APIs are now strictly workspace-scoped (build/preview/run/export history no longer leaks entries from other users on the same problem).
- Artifact download/browse/file endpoints now enforce workspace ownership for both build and preview artifact ids.
- Preview page now ignores foreign-workspace `preview_id` query values to prevent cross-workspace log/PDF detail leaks.
- Export UI now rejects cross-workspace `build_id` selections before export generation.
- Runner preflight now rejects cross-workspace `build_id` selections with deterministic failed-run metadata.
- Manifest API access is now workspace-scoped (`/api/problems/{problem}/workspaces/{user}/builds/{build_id}/manifest`), and the legacy unscoped route requires explicit workspace context.
- Request-context refresh optimization: `page_ctx` no longer performs redundant workspace status refreshes, and non-UI API/file routes skip branch-list resolution.
- Workspace bootstrap now supports optional status refresh with safe auto-refresh on newly created workspaces.
- Added DB indexes for workspace-scoped history queries and preview reuse lookup hot paths.
- Added direct `workspace_id` latest-row indexes for builds/previews to accelerate workspace header status queries.
- Branch-list API now degrades safely to current branch on git enumeration errors instead of returning 500.
- Run detail links now use workspace-scoped run-artifact endpoints (`/runs/{run_id}/artifacts/...` and `/runs/{run_id}/download-dir`) so replay files work for both valid builds and invalid preflight runs.
- Artifact and run-artifact browse/zip directory exports now perform symlink-safe walks (no symlink-directory traversal) and skip out-of-root resolved targets to prevent archive-path escape/exfiltration via crafted artifact trees.
- Run summaries now expose `feedback_dir` as a stable run-relative path (`feedback_dir`) instead of host-absolute filesystem paths.
- Workspace/branch switch routes now normalize page targets server-side (`artifacts`→`build`, `runs`→`run`, invalid→`files`), so redirects remain valid even without client-side page-target JS.
- Build/Run detail pages now handle malformed `summary_json` rows defensively (no 500 on corrupted metadata; fallback error shown in UI).
- Run artifact endpoints now validate filesystem-relative run-root shape (`<build>/logs/run-<run_id>` under problem artifacts, or `invalid-runs/<run_id>`) independent of DB-provided build-id strings, and reject DB-poisoned path overrides.
- Build detail log loading now uses canonical artifact roots (`artifacts/<problem>/<build_id>/logs`) instead of DB-provided `artifact_path` values.
- Preview reuse now loads candidate artifacts from canonical roots (`artifacts/<problem>/<preview_id>`), rejects dotted/traversal-like preview ids, and ignores DB-provided preview `artifact_path` values.
- Build artifact paths now enforce canonical artifact-id roots, rejecting dotted/traversal-like build ids across artifact browse/file/manifest flows.
- Files upload endpoint now streams uploads to disk in chunks to avoid buffering whole files in memory.
- Run execution upload flow now streams uploaded submissions directly to run artifacts instead of buffering entire files in request memory.
- Build and Preview pages now decode logs with UTF-8 replacement semantics, avoiding 500s on non-UTF8 tool output.
- Files page now sanitizes `line` query parsing and defaults invalid/non-positive values instead of raising 500.
- Build generator execution now streams output directly into test files to avoid buffering full generated tests in memory

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
  - Covers pass-fail, multi-pass, and interactive run flows (including interactive RE stderr transcript capture), snapshot clean/dirty path behavior, commit-ref canonicalization, invalid-commit build/preview failure persistence, commit/workspace-head preview artifact reuse, `compile_jobs`/`validate_jobs`/`solve_jobs`/`run_jobs` propagation, validate/solve/run worker effectiveness reporting, compile cache reuse/invalidation checks, manual sidecar answer-file filtering, missing-submission failure handling, invalid-build/missing-artifacts/missing-tests-dir preflight handling, failed-build export rejection, repeated-export filename uniqueness, polygon-export snapshot independence, workspace-history isolation across pages/APIs, workspace path-boundary rejection, cross-workspace artifact/preview/run/export rejection, workspace-scoped manifest API isolation, run-artifact endpoint coverage (including invalid-preflight runs), symlink-safe artifact zip/browse hardening, run-summary feedback path normalization, switch-page normalization robustness, malformed summary-json resilience on build/run pages, strict run-artifact root poisoning rejection checks (including traversal-style build-id poisoning), build-page artifact-path poisoning rejection checks, preview-reuse artifact-path/id poisoning rejection checks, dotted build-id artifact-root rejection checks (browse/manifest/detail), chunked file-upload binary round-trip checks, streamed run-upload route execution checks, non-UTF8 build/preview log rendering resilience, files-page invalid-line query resilience, and export zip structure checks.
