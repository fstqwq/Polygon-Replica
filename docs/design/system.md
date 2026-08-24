# System design

## Runtime

The application runs as one FastAPI process under uvicorn. Routes handle transport, implementation modules coordinate use cases, and services own domain behavior. One application runtime provides SQLite, Git, filesystem, sandbox, judgehost, worker, and maintenance dependencies.

The [execution protocol](../protocol/execution.md) defines queued work, restart reconciliation, and execution identity. The [storage protocol](../protocol/storage.md) defines filesystem ownership and cleanup. Supported process topology is documented in [runtime operations](../operations/runtime.md).

## State model

Publishing a workspace creates a published revision. Delivery follows `published revision -> native package -> external packages`; adapters never use a workspace or another adapter's output. Statement preview can render workspace or native package source. The [state lifecycle](state-lifecycle.md) defines freeze and invalidation points, and the [problem source protocol](../protocol/problem-source.md) defines authored layout.

## Trust boundaries

Browser sessions, agent credentials, and judgehost credentials are separate. The [access model](access.md) defines resource capabilities. Agent scopes can narrow a user's authority but cannot carry browser sudo.

The [judgehost wire protocol](../protocol/judgehost.md) defines judgehost trust, authentication, leases, callbacks, and version telemetry.
