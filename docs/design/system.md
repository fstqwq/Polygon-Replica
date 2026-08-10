# System design

## Runtime

The application runs as one FastAPI process under uvicorn. Routes delegate to
implementation modules in `app/impl`, which coordinate domain services in
`app/service`. SQLite access, Git operations, filesystem stores, sandbox
processes, and Judgehost state are wired by the runtime composition layer.

Request-path and queued work, restart reconciliation, and execution identity are
owned by the [execution protocol](../protocol/execution.md). Filesystem cleanup
and availability are owned by the [storage protocol](../protocol/storage.md).
The process topology and launcher constraints are described in
[runtime operations](../operations/runtime.md).

## Source lifecycle

A problem has a bare Git repository. The published revision is the commit at its
`main` reference. Each user workspace is a mutable checkout with its own status.
Publishing reconciles the workspace and moves the published Git reference; it
does not copy the committed source into SQLite.

Problem source layout is defined by the [problem source protocol](../protocol/problem-source.md).
Derived execution and package products are described by the
[storage](../protocol/storage.md) and [execution](../protocol/execution.md)
protocols.

## Trust boundaries

Browser sessions, agent tokens, and Judgehost credentials are distinct.
Authorization is enforced at HTTP boundaries before domain work. Agent tokens
cannot acquire, inherit, or present sudo authority. Sudo belongs only to the
browser session that completed elevation and is not transferable.

Judgehost trust, authentication, leases, callbacks, and version telemetry are
owned by the [Judgehost wire protocol](../protocol/judgehost.md).
