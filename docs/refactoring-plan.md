# Refactoring Plan

This file is a proposal document. It does not describe the current implementation.

Read `architecture.md`, `data-model.md`, and `verification-and-runs.md` first if you need the current system shape.

## Current Snapshot Before Further Refactoring

The following changes are already true in the codebase and should not be treated as open plan items:
- verification rows use `signature` and durable `kind` values `all`, `sample`, `custom`
- top-level removed-route compatibility is not a goal
- fresh verification detail downloads use `/artifacts/{verification_id}/...`
- the old `/runs/{run_id}/artifacts/...` route is gone
- verification result bytes are represented by refs:
  - `verification_tasks.output_ref`
  - verification metadata `input_ref`
  - verification metadata `answer_ref`
- solve-output cache has been removed; only exact case cache remains

This plan is for remaining structural cleanup.

## Remaining Structural Problems

### 1. Verification and judgehost are still too tightly coupled

There is still direct knowledge sharing between the verification pipeline and the judgehost adapter. The boundary is better than before, but task-finalization, task-row shaping, and callback behavior are still spread across both sides.

### 2. RuntimeConfig still owns too much concurrency state

`RuntimeConfig` still contains preview, export, verification, and login-rate-limit locks or inflight state. It is still a global wiring and concurrency object.

### 3. `impl/workspace/verification_dag.py` still holds domain-heavy logic

The verification DAG builder and executor still live in `impl/`, even though they perform service-level work.

### 4. Judgehost internals are still oversized

Judgehost logic is split across several large internal modules. The old mixin tower is no longer the right description, but the bounded-context split is still incomplete.

### 5. Some large UI context builders still need splitting

Run-detail context assembly is smaller than before but still carries too much formatting and read-model logic in one place.

## Target State

The next-stage target is:
- `impl/` only orchestrates requests and builds responses
- `service/verification/` owns DAG construction, execution, and aggregation
- `service/judgehost/` owns queueing, case-cache, DOMjudge protocol translation, and callbacks through explicit interfaces
- `RuntimeConfig` becomes a service registry, not a concurrency-state owner

## Phase 1: Tighten the verification-judgehost boundary

Goal:
- verification consumes task results and artifact refs
- judgehost emits task/case events through a narrow callback interface
- remove direct cross-module leakage where possible

Concrete direction:
- define an explicit callback protocol for case leased, case reported, and task terminal
- move shared row-shaping helpers to a neutral module
- keep judgehost from importing verification internals directly

## Phase 2: Move job concurrency state into services

Goal:
- preview, export, verification, and auth rate limiting manage their own concurrency state
- `RuntimeConfig` stops owning locks and inflight sets

Concrete direction:
- introduce a reusable job-guard utility
- move preview/export/verification inflight tracking into the owning service
- move login rate limiting into auth service

## Phase 3: Move verification DAG logic into `service/verification/`

Goal:
- `impl/workspace/verification_dag.py` becomes a thin wrapper or disappears
- DAG construction, execution, and result aggregation live under `service/verification/`

Concrete direction:
- split DAG building
- split execution loop
- split result summarization
- keep request handlers thin

## Phase 4: Continue splitting judgehost internals by responsibility

Goal:
- make judgehost internals testable by responsibility instead of by giant file

Concrete direction:
- isolate queue state ownership
- isolate DOMjudge protocol translation
- isolate result-processing and cache interaction

## Phase 5: Split remaining oversized UI read-model code

Goal:
- reduce large page-context builders
- keep formatting helpers near the page that uses them
- make run-detail read-model assembly easier to test directly

## Verification Checklist for Any Phase

After each phase:
- `python -c "import app.main"`
- targeted tests for the touched domain
- verify fresh run/verification detail pages in the browser
- verify no deleted route or old path model accidentally returns

## Non-Goals

These are not goals of this plan:
- preserving removed routes for backward compatibility
- preserving old data shapes once a new shape is chosen
- restoring the deleted `/runs/...` model
