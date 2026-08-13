# System design

## Runtime

The application runs as one FastAPI process under uvicorn. Routes delegate to
implementation modules in `app/impl`, which coordinate domain services in
`app/service`. The top-level `app.runtime.ApplicationRuntime` constructs the
SQLite, Git, filesystem, sandbox, Judgehost, worker, and maintenance objects.
`app.main.create_app()` installs that exact object on one FastAPI application;
`app/runtime_lifecycle.py` receives it explicitly for startup and shutdown.
Request implementation code uses the request-bound accessor in
`app/impl/runtime/dependency.py`; services and background work receive their
dependencies directly.

Request-path and queued work, restart reconciliation, and execution identity are
owned by the [execution protocol](../protocol/execution.md). Filesystem cleanup
and availability are owned by the [storage protocol](../protocol/storage.md).
The process topology and launcher constraints are described in
[runtime operations](../operations/runtime.md).

## State model

Workspaces publish official problem versions. Package delivery then follows the
single boundary `published source -> verified revision -> projections`.
DOMjudge and ICPC packages, plus Contest statement and package outputs, consume
the verified revision rather than a workspace or one another. Their inputs,
freeze points, invalidation rules, and cleanup behavior are described in
[state derivation and lifecycle](state-lifecycle.md). The authored workspace
layout is defined by the
[problem source protocol](../protocol/problem-source.md).

## Trust boundaries

Browser sessions, agent tokens, and Judgehost credentials are distinct.
Cross-resource capability decisions are owned by the access service and are
described in the [access model](access.md). Agent tokens cannot acquire,
inherit, or present sudo authority. Sudo belongs only to the browser session
that completed elevation and is not transferable.

Judgehost trust, authentication, leases, callbacks, and version telemetry are
owned by the [Judgehost wire protocol](../protocol/judgehost.md).
