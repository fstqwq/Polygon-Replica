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
- [Package](package.md) defines package import, native package identity, external
  formats, and contest bundles.
- [Statement preview](statement-preview.md) defines problem HTML/PDF/LaTeX
  preview from workspace or native package source, contest review, and transient
  full-contest PDF behavior.

Python module placement is documented in the
[application package map](../src/README.md).
