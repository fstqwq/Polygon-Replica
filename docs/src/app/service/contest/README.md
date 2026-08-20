# `app/service/contest`

Owns Contest identity, membership, typed metadata, problem-index-ordered roster,
statement source and attachments, build jobs, frozen build inputs, and
Contest-owned derived products. Relational state lives in `contest_*` tables;
durable authored content lives below the Contest source root; build products
live below `artifacts_root/contests`.

`ContestProblemQueryService` authorizes the complete roster in one batch before
touching a workspace and assembles its source-review and readiness rows. For
package readiness it compares each current published revision with the highest
available Native Package and reports `current`, `stale`, or `none`.

The normalized problem `idx` is both roster identity and order. The shared
natural comparator orders Excel-style letter indices before other custom
indices; no rank or position is stored. Build admission uses one SQLite writer
transaction to check active work, read that roster, select each problem's
highest available Native Package, and insert the job and all build items
with frozen `idx`, consecutive derived `ordinal`, and archive checksums.
A missing Native Package returns `not_ready` without a job, source snapshot,
or Verification. Contest workers never prepare or repair a Native Package,
call `ExportService`, or create problem-level export rows.

A synchronous Contest package download may request any registered external
adapter. Its `NativePackageReader` instances are opened once and shared across
all child builds. Every adapter receives the frozen roster `idx` and ordinal;
DOMjudge uses them as its short name and balloon palette position. Temporary
child ZIPs exist only inside the request and never enter the problem
external-package cache. The bundle is all-or-nothing and is deleted after the
response. The exact lifecycle is owned by the
[package protocol](../../../../protocol/package.md) and storage by the
[storage protocol](../../../../protocol/storage.md).

`ContestStatementPreviewService` owns blocking HTML Review orchestration and
transient PDF Preview orchestration. PDF Preview reuses the complete Contest TeX
compiler to produce one document; it is not a Contest job or artifact. The
[Statement Preview protocol](../../../../protocol/statement-preview.md) owns
that cache lifecycle.
