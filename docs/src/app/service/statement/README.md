# `app/service/statement`

Owns statement language discovery, source interpretation, FreeMarker rendering, TeX compilation, Pandoc HTML conversion, structured samples, and preview records.

Inputs are workspace or native package statement source, problem limits, and sample evidence. Outputs are rendered TeX trees, HTML fragments, PDFs, logs, and preview read models. Authored files remain source; generated trees and preview payloads are cache or native package content.

Preview cache lookup reuses the selected actor-scoped record to validate its
payload path. HTML text is read when the caller requests the fragment. Payload
validation and retrieval share the same regular-file and root-confinement checks.
Pandoc output is sanitized before `content.html` is persisted, so direct and
cached HTML reads return the same safe fragment. Preview metadata is narrowed
to typed summaries at the SQLite JSON boundary.

The TeX presentation preprocessor scans each input in linear time, preserving
comments, escapes, and verbatim regions while identifying supported layout
commands. It recognizes environment names with bounded comparisons and copies
only consumed source spans. Pandoc owns the subsequent LaTeX parsing.

Problem and contest rendering share one sample model for pair, multi-pass, interactive, and multi-pass interactive examples. The optional authored `statement/examples.tex` overrides the canonical fallback and is rendered beside `problem.tex`.

The [problem source protocol](../../../../protocol/problem-source.md) owns authored layout. The [statement preview protocol](../../../../protocol/statement-preview.md) owns rendering, identity, sandbox, and cache behavior.

`sample_markup.py` validates optional testcase annotations against displayed
bytes and projects one shared line selection for HTML and PDF. `examples.py`
selects execution evidence, including custom display output, and render-tree
writing emits numeric sidecars. The statement preview protocol owns fallback
and coloring semantics.

PDF sample controls use Problem-local TeX state. The HTML converter initializes
each Problem from its preview default and applies literal source controls before
sample rendering. Both retain execution markup when presentation is disabled.
