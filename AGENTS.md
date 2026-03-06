# AGENTS.md

## Purpose

This repository implements a local Polygon-like problem authoring system.

Engineering baseline:

- Git is the single source of truth for problem sources.
- Derived artifacts stay on local filesystem (not in Git).
- Web UI is the primary workflow entry.
- No backward-compat target for removed routes or old data shapes.

## Runtime Model

- bare repository: `/srv/git/<owner>/<slug>.git`
- per-user workspace: `/srv/workspaces/<viewer>/<owner>/<slug>/`
- build/preview artifacts: `/var/lib/polygonlike/artifacts/objects/<hh>/<ref>/`
- run artifacts: `/srv/runs/<run_id>/`
- judgehost temp work root: `/srv/runs/judgehost-domjudge/<task_id>/`
- metadata DB: problems/users/workspaces/builds/runs/exports/audit/...

Rules:

- DB stores metadata; payload files stay on filesystem.
- run/verification/export/contest jobs are async worker jobs.
- preview compile is synchronous in request path.
- judgehost API surface is `/api/v4/*`.
- async/judge fs caches are startup-cleared by current runtime policy.

## Active Docs

Active project docs live at repository root:

- `AGENTS.md`
- `PERMISSION.md`
- `BACKEND_TODO.md`
- `ASYNC_WORKER_PLAN.md`
- `PROGRESS.md`

## Refactor Rule
- For files larger than 1000 lines, consider refactor to split into smaller files.
- Use subdirectories or even subsubdirectories as needed to maintain a clean structure.
- Any refactor must define responsibility boundaries and invariants first.
- If boundary/invariant cannot be stated clearly, reject the split.
