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
  - Covers pass-fail, multi-pass, and interactive run flows, `compile_jobs`/`run_jobs` propagation, run worker effectiveness reporting, compile cache reuse/invalidation checks, manual sidecar answer-file filtering, missing-submission failure handling, invalid-build/missing-artifacts preflight handling, workspace path-boundary rejection, and export zip structure checks.
