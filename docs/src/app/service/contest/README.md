# `app/service/contest`

Owns contest identity, membership, properties, canonical problem-index roster, statement source and attachments, readiness, statement preview orchestration, and contest package bundles.

Contest metadata is stored in SQLite; authored TeX and attachments live below the contest source root. `idx` is both roster identity and natural order. Readiness compares each current published problem revision with its native package after authorizing the complete roster.

Statement review produces blocking HTML or transient PDF previews from workspace or native package source. Package download freezes the ready native packages, prepares or reuses the selected external format, applies contest placement to extracted copies, and returns an all-or-nothing temporary bundle with complete common-language statements.

The [package](../../../../protocol/package.md), [statement preview](../../../../protocol/statement-preview.md), and [storage](../../../../protocol/storage.md) protocols define the corresponding lifecycles.
