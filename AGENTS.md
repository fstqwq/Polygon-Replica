# AGENTS.md

## Purpose

This repository implements a local Polygon-like problem authoring system.

Current engineering baseline:

- Git is the single source of truth for problem sources.
- Derived artifacts stay on local filesystem (not in Git).
- Web UI is the primary workflow entry (Problems/Contests -> Problem submenu).
- No legacy/backward-compat compatibility target for removed routes/data shapes.

## Canonical Upstreams

- testlib: <https://github.com/MikeMirzayanov/testlib>
- ICPC Problem Package Format: <https://github.com/icpc/problem-package-format>
- Polygon behavior reference: <https://polygon.codeforces.com/>

Policy in this repository:

- `third_party/upstream/testlib/testlib.h` is a maintained ICPC-package-compatible variant used by this project.
- Workspace-seeded `third_party/testlib/testlib.h` is copied from that maintained upstream directory.
- Export behavior and conformance expectations follow upstream problem-package-format.

## Documentation Layout

Markdown docs are kept at repository root:

- `AGENTS.md`
- `ASYNC_WORKER_PLAN.md`
- `BACKEND_TODO.md`
- `PROGRESS.md`

Do not place active design/ops markdown under `docs/`.

## Runtime Model

Core objects:

- bare repository (`/srv/git/<problem>.git`)
- per-user workspace (`/srv/workspaces/<user>/<problem>/`)
- artifact root (`/var/lib/polygonlike/artifacts/<problem>/<build_id>/`)
- run fallback root (`/srv/runs/invalid-runs/<run_id>/`)
- metadata DB (`problems/users/repo_acl/workspaces/builds/runs/exports/audit_log/...`)

The DB stores metadata; large files/logs/tests/answers/packages stay on filesystem.

## UI and Navigation Baseline

Top-level main menu:

- Problems
- Contests

Problem submenu (current implementation):

- General info
- Statement
- Files
- Generators
- Checker
- Validator
- Interactor
- Tests
- Solution files
- Invocations
- Packages
- Manage access

Cross-page behavior:

- Show current problem/workspace/revision/dirty state in problem context.
- Timestamps are shown in local time.
- Raw commit hash should not be shown in normal UI views (commit/history contexts excepted).

## Repository Structure (Problem)

Required versioned source layout:

- `statement/`
- `config/`
- `validators/`
- `checkers/`
- `interactors/`
- `generators/`
- `solutions/`
- `tests/spec.json`
- `tests/manual/<test_id>.in`
- `tests/generator/<test_id>.in`
- `third_party/testlib/testlib.h`

Tests model currently used:

- `tests/spec.json` stores metadata only (`id`, `kind`, `sample`)
- payload lives in `tests/manual/*.in` or `tests/generator/*.in`

## Build / Invocation / Export Rules (Current)

- Editing tests does not require explicit build.
- Opening Tests page must not auto-build.
- Invocation execution triggers background runnable snapshot resolution when needed.
- Run/verification/export are queued as async jobs (worker queue).
- Preview compile is currently synchronous.
- Export is ICPC-only and bound to committed `HEAD` revision (not dirty working copy).

Checker/validator/interactor state today:

- Checker supports standard checker metadata mode (`std::*`) and repository mode switch.
- Validator/interactor are currently repository-source workflow only.

## Sandbox and Security Baseline

- Single execution backend: `native-sandbox`.
- Root switch via `bwrap` is fail-closed at startup (if probe fails, service startup fails).
- Same-origin enforcement for state-changing requests.
- Session + flash cookies are `HttpOnly` with secure policy from runtime config.
- Workspace mutating operations are serialized by workspace-level locks.

## Local Dev and Validation

Before tests:

```bash
source .venv/bin/activate
```

Primary regression command:

```bash
./scripts/test.sh
```

It runs: `py_compile`, `pyflakes`, `vulture`, `unittest`.

## Update Rule

When behavior changes, update these files in the same PR:

- `AGENTS.md` (contract/baseline)
- `BACKEND_TODO.md` (remaining technical debt)
- `PROGRESS.md` (milestone/progress checkpoint)
