# `app/service/statement`

Owns statement language discovery, section and template interpretation, source
signatures, FreeMarker-subset rendering, TeX compilation, and preview records.
It consumes workspace statement source, problem limits, and testcase sample
metadata; it produces regenerated TeX trees, PDFs/logs, and preview read models.
The canonical template selects XeLaTeX and uses TeX Gyre Latin fonts plus Noto
CJK fonts; both host and Docker deployment install and probe those dependencies.

Authored inputs remain Git source. SQLite stores preview metadata, while preview
PDFs and logs are cache payloads. Preview compilation runs
synchronously and may run sample-only verification to hydrate missing sample
data in its snapshot.
The source layout is owned by the
[problem-source protocol](../../../../protocol/problem-source.md).
