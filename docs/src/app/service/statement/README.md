# `app/service/statement`

Owns statement language discovery, section and template interpretation, source
signatures, FreeMarker-subset rendering, TeX compilation, and preview records.
It consumes workspace statement source, problem limits, and testcase sample
metadata; it produces regenerated TeX trees, PDFs/logs, and preview read models.
The canonical template selects XeLaTeX and uses TeX Gyre Latin fonts plus Noto
CJK fonts; both host and Docker deployment install and probe those dependencies.

Language selection is source context, rendering interprets statement source
(including its title), and TeX compilation executes the external toolchain.
Titles do not have an independent service or lifecycle.

Authored inputs remain Git source. SQLite stores preview metadata, while preview
PDFs and logs are cache payloads. Preview compilation runs
synchronously and may run sample-only verification to hydrate missing sample
data in its snapshot.

The renderer evaluates `statement/problem.tex` and the optional
`statement/examples.tex` with one context. Missing examples source selects the
canonical `third_party/Polygon-WF-Styles/examples.tex`; an existing file is
read strictly and never replaced by fallback after a read or UTF-8 error. The
two rendered files are written together into the per-language compile tree.
The canonical problem template always inputs the rendered companion.
The Statement authoring page creates the canonical source only when the author
opts in, deletes it when the override is disabled, and deletes it when the core
templates are reset to defaults.

The default examples consumer prefers optional structured
`problem.examples.samples` data and otherwise projects existing
`problem.sampleTests`. This service currently produces only the Polygon
projection. Structured multipass and interaction data must be supplied by a
future producer or by another rendering caller; it is not persisted in
`tests/spec.json` by this layer.

The source layout is owned by the
[problem-source protocol](../../../../protocol/problem-source.md).
