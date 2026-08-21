# `app/service/contest`

Owns Contest identity, membership, editable properties, the canonical
problem-index roster, Statement Sources, attachments, readiness projections,
Statement Preview orchestration, and synchronous Contest package downloads.
Relational state lives in the canonical `contest_*` tables, while authored TeX
source and attachments live below the Contest source root.

The normalized problem `idx` is both roster identity and order. The shared
natural comparator orders Excel-style letter indices before other custom
indices; no rank or position is stored. `ContestProblemQueryService` authorizes
the complete roster in one batch before touching a Workspace and projects each
current published revision as `current`, `stale`, or `none` against its Native
Package.

`ContestStatementPreviewService` builds blocking HTML Review and transient PDF
Preview results from Workspace or Native Package sources. Preview payloads are
cache, not Contest artifacts.

A Contest package download opens every ready Native Package in canonical roster
order and invokes the selected registered adapter. DOMjudge receives each
problem's `idx` and ordinal for its short name and balloon color. Child archives
and the outer bundle are request-owned temporary files, are all-or-nothing, and
are deleted after transfer. The current service creates no Contest build job,
history, build item, artifact, or problem-level external-package cache entry.
The exact lifecycle is defined by the
[package](../../../../protocol/package.md),
[Statement Preview](../../../../protocol/statement-preview.md), and
[storage](../../../../protocol/storage.md) protocols.
