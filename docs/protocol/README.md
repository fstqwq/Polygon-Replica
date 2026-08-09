# Protocol index

- [Problem source](problem-source.md) owns the workspace file layout and source
  interpretation.
- [Execution](execution.md) owns verification/custom-run identity, DAG behavior,
  results, and artifact references.
- [Judgehost](judgehost.md) owns `/api/v4/*` authentication, leasing, files,
  telemetry, and callbacks.
- [Storage](storage.md) owns filesystem roots, locator resolution, availability,
  and cleanup.
- [Persistence](persistence.md) owns durable SQLite responsibilities and row
  lifecycles.
- [Package](package.md) owns Native, Polygon, and ICPC archive boundaries.

Protocol requirements use normative words only where an external producer,
consumer, or durable invariant must comply. Python module placement is documented
under [the application package map](../src/README.md).
