# Protocol index

- [Problem source](problem-source.md) describes the workspace file layout and
  how source files are interpreted.
- [Execution](execution.md) defines verification and custom-run identity, DAG
  behavior, results, and cache-payload references.
- [Judgehost](judgehost.md) defines `/api/v4/*` authentication, leasing, files,
  telemetry, and callbacks.
- [Storage](storage.md) defines source, derived, and cache lifecycles together
  with filesystem roots, locator consistency, and cleanup.
- [Persistence](persistence.md) describes durable SQLite state and row
  lifecycles.
- [Package](package.md) defines Native, Polygon, and ICPC archive boundaries.

Python module placement is documented in the
[application package map](../src/README.md).
