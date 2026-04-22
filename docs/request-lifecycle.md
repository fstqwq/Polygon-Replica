# Request Lifecycle and Auth

## Entry Point

`app/main.py` creates the FastAPI application, mounts static files at `/static`, and includes eight routers.

Current startup and shutdown hooks call:
- `app.impl.auth.internal.runtime.startup()`
- `app.impl.auth.internal.runtime.shutdown()`

Startup does more than auth wiring. It also:
- initializes metadata
- cancels inflight preview, contest, judgehost, and verification jobs
- clears cache-root state and the worker-queue durable log
- starts the worker queue

Shutdown stops the worker queue.

## HTTP Middleware

The top-level HTTP middleware:
1. records request start time
2. delegates to auth middleware
3. adds `X-Backend-Render-Ms` to the response

Auth middleware is responsible for:
- session validation
- attaching the current user to `request.state`
- CSRF/session enforcement for the web UI

## High-Level Request Flow

```mermaid
sequenceDiagram
    participant Browser
    participant MW as Middleware
    participant Route as route/*.py
    participant Impl as impl/*.py
    participant Service as service/*.py
    participant DB as SQLite
    participant TPL as Jinja2

    Browser->>MW: HTTP request
    MW->>Route: authenticated request
    Route->>Impl: handler
    Impl->>Service: domain call
    Service->>DB: read or write metadata
    Service-->>Impl: result
    Impl->>TPL: render page or redirect
    TPL-->>Browser: response
```

## Auth Model

### Web UI auth

The web UI uses session cookies stored in `auth_sessions`.

Current pieces:
- login session cookie
- sudo session cookie for destructive actions
- in-memory login rate limiting state on `RuntimeConfig`

### Judgehost auth

The judgehost API under `/api/v4/*` uses a separate auth path.
It does not use the browser session cookie.

Current judgehost auth accepts configured credentials for DOMjudge-compatible clients.

## Route Groups

### Root and auth
- `/login`, `/register`, `/setup`, `/sudo`, `/logout`
- `/`, `/problems`, `/contests`
- import entry points for problems and contests

### Problem workspace
- `/problems/{problem:path}/...`
- statement, generators, checker, validator, interactor, solutions, files, workspace, history, access
- git operations and settings pages

### Contest
- `/contests/{contest}/...`
- overview, problems, properties, access, packages

### Agent
- `/agent/sessions`, `/agent/connect`, approval, revoke, and disconnect pages
- `/agent/v1/*` registration, auth, workspace, verification, export, and commit APIs

### Preview
- statement preview pages and compile/recompile actions
- statement language add action under `/statement/languages/add`

### Run and export
- `/problems/{problem:path}/run`
- `/problems/{problem:path}/run/new`
- `/problems/{problem:path}/run/details`
- `/problems/{problem:path}/run/details/test-fragment`
- `/problems/{problem:path}/run/execute`
- `/problems/{problem:path}/run/rejudge`
- `/problems/{problem:path}/run/cancel`
- `/problems/{problem:path}/export`
- `/problems/{problem:path}/export/create`
- `/problems/{problem:path}/export/snapshot`
- `/problems/{problem:path}/export/import`
- `/problems/{problem:path}/export/import/slug-hint`
- `/problems/{problem:path}/artifacts/{verification_id}/{rel_path:path}`
- `/problems/{problem:path}/exports/{export_id}/{filename}`

There is no current `/runs/{run_id}/artifacts/...` route.

### Tests
- test-spec editing and verification actions

### Judgehost API
- `/api/v4/config`
- `/api/v4/languages`
- `/api/v4/judgehosts`
- `/api/v4/judgehosts/fetch-work`
- `/api/v4/judgehosts/get_files/source/{item_id}`
- `/api/v4/judgehosts/get_files/source/{contest_id}/{item_id}`
- `/api/v4/judgehosts/get_files/{file_type}/{item_id}`
- `/api/v4/judgehosts/get_version_commands/{judgetask_id}`
- `/api/v4/judgehosts/check_versions/{judgetask_id}`
- `/api/v4/judgehosts/update-judging/{hostname}/{judgetask_id}`
- `/api/v4/judgehosts/add-judging-run/{hostname}/{judgetask_id}`
- `/api/v4/judgehosts/add-debug-info/{hostname}/{judgetask_id}`
- `/api/v4/judgehosts/internal-error`

## Sync vs Async Work

Current runtime model:
- preview compile is synchronous in the request path
- verification jobs are async worker-queue jobs
- run execution is async and uses the verification task graph internally
- export jobs are async worker-queue jobs
- contest build/package jobs are async worker-queue jobs

## Statement Language Resolution

Statement requests use a two-step rule:
1. at the route/impl boundary, resolve the current language
2. pass that concrete language through the statement workflow

Current resolution source:
- language directories under `statement-sections/*`

Current default ordering:
- `english`
- `chinese`
- all remaining language directories alphabetically

Current rules by layer:
- `preview_page()` may pick a default language when the request URL has no `language`
- `preview_run()`, `preview_save()`, attachment actions, and export calls carry an explicit `language`
- render and preview services consume explicit language tokens
- preview cache keys and preview summaries are partitioned by language

There is no current `statement/language.txt` request dependency.

## Statement Request Flow

```mermaid
sequenceDiagram
    participant Browser
    participant Impl as app/impl/preview/preview.py
    participant Ctx as app/service/statement/context.py
    participant Preview as app/service/statement/preview.py
    participant Render as app/service/statement/render.py

    Browser->>Impl: GET /statement?language=...
    Impl->>Ctx: discover languages / pick default if needed
    Impl-->>Browser: HTML with current language
    Browser->>Impl: POST save or compile with hidden language
    Impl->>Preview: compile_preview(..., language)
    Preview->>Render: render_statement_main(..., language)
    Render-->>Preview: statement/main.tex
    Preview-->>Browser: redirect with language + preview_id
```

## Verification/Run Detail Downloads

Current run-detail downloads all go through verification artifact routes.

Examples:
- `tests/{test_name}`
- `ans/{answer_name}`
- `output/{task_id}/{file_name}`
- `blob/{encoded-token}/{file_name}`

Resolution is handled in `app/impl/workspace/artifact.py` and served by `app/impl/run_export/artifact.py`.

## Templates and Static Files

Templates live in `app/template/` and are rendered through the `Jinja2Templates` instance on `RuntimeConfig`.

Static assets live in `app/static/` and are mounted at `/static`.
