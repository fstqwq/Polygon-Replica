# AGENTS.md

## Purpose

This repository implements a local Polygon-like problem authoring system.

Engineering baseline:

- Git is the single source of truth for problem sources.
- Derived artifacts stay on local filesystem (not in Git).
- Web UI is the primary workflow entry.
- No backward-compat target for removed routes or old data shapes.

Remember that Claude Opus 4.6 will review your code.

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

## Python code style

- Use clear type hints everywhere.
- Reduce runtime type checks by strengthening upstream types and boundaries first.
- Do less `isinstance`, `is not None`, `if x:`, etc. To do so, use strict types in the upstream code.
- Do not use synonym substitution like `if x is not None` vs `if x` vs `if x != ""` vs `if x != []` vs `if x != {}`. Instead, upstream code should guarantee that the value is always a canonical shape (e.g. never `None`, always a list, etc.) so that downstream code can just consume it without extra checks.
- Boundary code may validate and normalize external input once. Internal code should consume canonical shapes directly.
- Once a token is canonical inside the system, do not keep re-normalizing it with `.strip()`, `.lower()`, `str(...)`, or similar compatibility coercion.

## Active Docs

Active project docs live at repository root:

- `AGENTS.md`
- `PERMISSION.md`
- `BACKEND_TODO.md`
- `ASYNC_WORKER_PLAN.md`
- `PROGRESS.md`

## Refactor Rule

Never consider backwards compatibility. Always prefer risky refactor and code removal over maintaining old code. If the code is not needed, remove it. If the code is needed but can be improved, refactor it. If the code is needed and cannot be improved, keep it as is.

- Never use "patch" to fix the problem. Always consider use a simple, unified way to solve the problem.
- For files larger than 1000 lines, consider refactor to split into smaller files.
- Use subdirectories or even subsubdirectories as needed to maintain a clean structure.
- Any refactor must define responsibility boundaries and invariants first.
- If boundary/invariant cannot be stated clearly, reject the split.
