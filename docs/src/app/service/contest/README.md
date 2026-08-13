# `app/service/contest`

Owns Contest identity, membership, typed metadata, ordered problem roster,
statement source and attachments, build jobs, frozen build inputs, and
Contest-owned derived products. Relational state lives in `contest_*` tables;
durable authored content lives below the Contest source root; build products
live below `artifacts_root/contests`.

`ContestProblemQueryService` authorizes the complete roster in one batch before
touching a workspace and assembles its source-review and readiness rows. For
package readiness it compares each current published revision with the highest
available verified revision and reports `current`, `stale`, or `none`.

Build admission uses one SQLite writer transaction to check active work, read
the ordered roster and labels, select each problem's highest available verified
revision, and insert the job and all build items with frozen archive checksums.
A missing verified revision returns `not_ready` without a job, source snapshot,
or Verification. Contest workers never prepare or repair a verified revision,
call `ExportService`, or create problem-level export rows.

A job may request statement PDF, DOMjudge bundle, and ICPC 2025-09 bundle. Its
verified-revision readers are opened once and shared across selected outputs.
Statement assembly reads source, assets, and samples directly from those
readers. Package bundles invoke the common pure projector for every problem;
the DOMjudge projection receives the frozen roster label as its short name.
Temporary child ZIPs exist only inside the Contest job and never enter the
problem projection cache.

Each bundle is all-or-nothing, while different output types are independent and
can produce a `partial` job. Package-only work does not snapshot Contest source.
Generated-data cleanup may remove jobs and products without deleting Contest
metadata, membership, roster, or authored source. The exact lifecycle is owned
by the [package protocol](../../../../protocol/package.md) and storage by the
[storage protocol](../../../../protocol/storage.md).
