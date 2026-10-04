# `app/service/contest`

Owns contest identity, membership, properties, canonical problem-index roster, statement source and attachments, readiness, statement preview orchestration, and contest package bundles.

Contest metadata is stored in SQLite; authored TeX and attachments live below the contest source root. `idx` is both roster identity and natural order. Readiness compares each current published problem revision with its native package after authorizing the complete roster.

Property parsing distinguishes localized template values from per-problem marks.
Marks share the property store and mutation lifecycle; statement projections
exclude them. The [statement preview protocol](../../../../protocol/statement-preview.md)
defines their key and save semantics.

Statement review produces blocking HTML or transient PDF previews from workspace or native package source. Package download freezes the ready native packages, prepares or reuses the selected external format, and returns an all-or-nothing temporary bundle with complete common-language statements. DOMjudge placement changes an extracted copy; other formats retain the cached archive bytes after validating an isolated copy.

The [package](../../../../protocol/package.md), [statement preview](../../../../protocol/statement-preview.md), and [storage](../../../../protocol/storage.md) protocols define the corresponding lifecycles.

Add-problem search matches the full problem slug as a case-insensitive literal
substring. Direct write access, roster exclusion, and search filtering precede
the result limit in the database query.
