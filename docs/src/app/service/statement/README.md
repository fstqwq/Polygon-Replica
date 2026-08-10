# `app/service/statement`

Owns statement language discovery, section and template interpretation, source
signatures, FreeMarker-subset rendering, TeX compilation, and preview records.
It consumes workspace statement source, problem limits, and testcase sample
metadata; it produces regenerated TeX trees, PDFs/logs, and preview read models.

Authored inputs remain Git source. SQLite stores preview metadata, while preview
PDFs and logs are cleanup-safe cache artifacts. Preview compilation runs
synchronously and may run sample-only verification to hydrate missing sample
data in its snapshot.
The source layout is owned by the
[problem-source protocol](../../../../protocol/problem-source.md).
