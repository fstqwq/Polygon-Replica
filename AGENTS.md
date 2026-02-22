# AGENTS.md

## Purpose

This repository defines a Polygon-like problem authoring system with Git as the single source of truth, local filesystem storage for build outputs, support for `testlib.h`, and TeX statement compilation with preview. The system includes a web UI that exposes the core workflow end-to-end.

## Upstream References

The system uses these upstream references as canonical sources for required assets and format behavior:

- `testlib`: https://github.com/MikeMirzayanov/testlib
- `Kattis Problem Package Format`: https://github.com/Kattis/problem-package-format

Operational policy:

- `third_party/testlib/testlib.h` in seeded problem repositories must be sourced from upstream `testlib`.
- Export/format behavior, schemas, and regression examples for Kattis packaging should be derived from upstream `problem-package-format` spec/examples.
- When required files/tests are missing locally, obtain or refresh them from these upstream repositories.

## Progress Tracking

- Current implementation status is tracked in `PROGRESS.md`.
- Keep `PROGRESS.md` updated whenever milestone status or validation state changes.

## System Overview

### Core Concepts

- **Problem Repository (Git, per problem)**: Stores all editable source files (statements, generators, validators, checkers, interactors, solutions, configs).
- **Bare Repository (central)**: Canonical Git remote for each problem.
- **User Workspace (per user, per problem)**: A server-side working copy used for editing, committing, pushing/pulling, and merging.
- **Ephemeral Run Directory (per job)**: Immutable snapshot of a selected commit/workspace state used for builds/runs.
- **Artifacts Store (local filesystem)**: Stores derived outputs (tests/ans/logs/export zips/preview PDFs) indexed by `build_id`.
- **Database (metadata only)**: Tracks problems, users, ACL, workspaces, builds, runs, exports, and audit logs.

### Directory Layout (Host)

- Bare repos: `/srv/git/<problem>.git`
- User workspaces: `/srv/workspaces/<uid>/<problem>/`
- Job workdirs: `/srv/runs/<run_id>/`
- Artifacts: `/var/lib/polygonlike/artifacts/<problem>/<build_id>/...`
- Caches: `/var/cache/polygonlike/...`

### Web UI Navigation (Required)

The web UI must expose the following sections within a problem workspace:

- Files
- Git
- Build
- Preview
- Run
- Export

The UI must display, at all times:

- Current problem
- Current workspace (user)
- Current branch and HEAD commit
- Uncommitted changes indicator
- Recent build/preview status

## Problem Repository Specification

Each problem repository must follow this minimal structure:

- `statement/` (TeX statement sources and assets)
- `config/` (problem configuration and build configuration)
- `validators/` (input validator sources)
- `checkers/` (output checker sources)
- `interactors/` (interactor sources for interactive problems)
- `generators/` (test generators)
- `solutions/` (reference solutions; must include accepted solution)
- `tests/manual/` (handwritten tests; may include sample tests)
- `third_party/testlib/testlib.h` (fixed `testlib.h` copy)

All of the above are versioned in Git. No derived artifacts are stored in the repository.

## Database Schema (Minimum)

The system must maintain at least these logical tables (implementation may differ):

- `problems`
- `users`
- `repo_acl`
- `workspaces`
- `builds`
- `runs`
- `exports`
- `audit_log`

The database stores metadata only. Large files are stored on the local filesystem under the artifacts directory.

## Artifacts Specification (Local Filesystem)

Artifacts are written per build under:

`/var/lib/polygonlike/artifacts/<problem>/<build_id>/`

Required subpaths:

- `manifest.json`
- `tests/`
- `ans/`
- `logs/`
- `statement_preview/`
- `export/`

`manifest.json` must include:

- source identifier: commit SHA (and branch/ref if applicable)
- toolchain identifier/digest
- seed and generation parameters
- file listing with sha256 and sizes
- summary statistics (counts, sizes)
- step list with status and log locations

## Compilation and Toolchain Rules

### testlib.h

- `testlib.h` must be read from `third_party/testlib/testlib.h` inside the repository.
- Validator, checker, and interactor builds must support `#include "testlib.h"` by adding the repository `third_party/testlib/` include path.

### Unified Compiler Configuration

A single toolchain configuration must apply to:

- generators
- validators
- checkers
- interactors
- solutions

The system must support deterministic compilation with caching:

- Cache key includes `(toolchain_digest, source_hash)`.
- Cache may store final executables and/or intermediate objects.

UI requirements:

- Build/Run pages must surface compiler diagnostics.
- Error entries must link to source file path and line number and open the file in the Files editor.

## TeX Statement Compilation and Preview

### Inputs

- `statement/*.tex` plus assets under `statement/` (and subdirectories such as `figures/`).

### Outputs

Under `artifacts/<problem>/<build_id>/statement_preview/`:

- `statement.pdf`

Under `artifacts/<problem>/<build_id>/logs/`:

- `latex.log`

### Behavior

- TeX compilation must be triggered from the UI Preview page for either:
  - workspace HEAD, or
  - a specified commit
- The PDF must be viewable in the UI, and `latex.log` must be displayed.
- Log entries must allow navigation to the referenced file and line within the repository.

## Build Pipeline (tests/ans generation)

A build is identified by `build_id` and binds to a specific source state (commit SHA, or workspace snapshot).

### Build Steps (Required)

1. Compile:
   - generator
   - validator
   - checker
   - accepted solution
2. Generate `tests/`:
   - include `tests/manual/`
   - generate additional tests using generators per repository configuration
3. Validate inputs:
   - run the validator for each test
4. Generate `ans/`:
   - run accepted solution over each test to produce corresponding answer
5. Persist:
   - write `manifest.json`
   - write per-step logs and per-test failure details

UI requirements (Build page):

- Trigger build for workspace HEAD or a specific commit.
- Show step list (compile/generate/validate/solve) and per-step logs.
- On failure, identify the step and the failing test id.
- Provide browse/download access to `tests/` and `ans/` for a `build_id`.

## Runner (Execution Engine)

The runner executes submissions against a selected `build_id`.

### Supported Modes

- pass-fail
- interactive
- multi-pass (driven by `feedback_dir/nextpass.in`)

### Required Outputs per Run

- Verdict per test case
- Resource usage per test case (and per pass where applicable)
- `feedback_dir` snapshot
- Interactive transcript where applicable

UI requirements (Run page):

- Select `build_id` and submission source (from workspace or upload).
- Execute and display results per test case.
- Provide transcript view/download for interactive.
- Provide browse/view for key feedback files (e.g., `judgemessage.txt`, `teammessage.txt`).

## Exporters (DOMjudge / Kattis / Polygon)

For a given `build_id`, the system must generate downloadable zip packages.

### Required Export Types

- Kattis problem package format zip
- DOMjudge legacy-icpc zip
- Polygon zip:
  - standard
  - full (includes `tests/` and `ans/`)

### Storage

Export zips are written under:

`artifacts/<problem>/<build_id>/export/`

UI requirements (Export page):

- Trigger export generation for a selected `build_id`.
- List available export zips with:
  - filename
  - size
  - sha256
  - generation time
  - source commit SHA

## Web UI Requirements (Cross-cutting)

Across all pages:

- Always show current problem, workspace, branch, HEAD commit, and dirty state.
- Provide a consistent way to switch:
  - workspace
  - branch
  - commit (where applicable)
- Ensure all operations that mutate workspace state are serialized with workspace-level locking.

## Milestone Plan

### Milestone 0: Engineering Skeleton + UI Skeleton

- Define repository structure and host directory layout.
- Initialize database schema (minimum tables).
- Implement web UI routing and workspace selection.
- Implement read-only APIs for problems/workspaces/branches/status/recent builds.

**Done when**: A user can open a problem workspace in the browser and see correct Git status and navigation.

### Milestone 1: Git Workflow (per-user working copy) + Web Editing

- Create bare repo per problem and initialize `main`.
- Create user workspace clones under `/srv/workspaces/<uid>/<problem>/`.
- Implement file operations in workspace:
  - browse, edit, create, delete, rename, upload, download
- Implement Git operations in workspace:
  - status/diff, commit, push, pull, branch create/switch, merge into `main` with conflict reporting
- UI: Files and Git pages implement the above.

**Done when**: Multiple users can edit and commit in isolated workspaces, push to bare, and merge into `main` via UI with conflict visibility.

### Milestone 2: Local Artifacts Store + Build Records + UI Visibility

- Implement artifacts directory structure per `build_id`.
- Persist build metadata and write `manifest.json`.
- UI: Build list and build detail views (status, summary, log index).

**Done when**: A build creates artifacts on disk and the UI can list and inspect build metadata.

### Milestone 3: `testlib.h` Support + Unified Toolchain + UI Diagnostics

- Enforce `third_party/testlib/testlib.h` include.
- Implement unified compilation rules and compile caching.
- UI: Present compiler diagnostics and link to file/line in editor.

**Done when**: testlib-based validator/checker/interactor compile and run; compile cache is reused.

### Milestone 4: TeX Preview + UI Preview Page

- Implement TeX compilation to `statement.pdf` and store logs.
- UI: Trigger preview; render PDF; display log with navigation to source.

**Done when**: TeX sources can be iterated and previewed from the UI with actionable error output.

### Milestone 5: Build Pipeline (tests/ans) + UI Build Panel

- Implement compile → generate → validate → solve pipeline.
- Store outputs under `tests/` and `ans/`, with step logs and per-test failure details.
- UI: Trigger build; inspect step logs; browse/download generated data.

**Done when**: A repository state can be built into a complete test set with answers and debuggable logs.

### Milestone 6: Runner (pass-fail / interactive / multi-pass) + UI Run/Replay

- Implement runner supporting:
  - pass-fail sequential validator invocation
  - interactive concurrent IO broker with transcript
  - multi-pass controlled by `feedback_dir/nextpass.in`
- Persist run results and artifacts.
- UI: Trigger runs; display verdicts and resources; view transcript and feedback files.

**Done when**: All supported modes execute end-to-end and results are viewable and replayable in the UI.

### Milestone 7: Exporters + UI Export Page

- Implement zip exporters:
  - Kattis
  - DOMjudge legacy-icpc
  - Polygon standard and full
- Store exports under `export/` with hashes.
- UI: Generate and download export packages with provenance.

**Done when**: A `build_id` can be exported to all formats and downloaded from the UI.

## Non-goals

- Storing derived artifacts in Git.
- Requiring external object storage for artifacts.
- Defining optional feature sets beyond the specified milestones.
