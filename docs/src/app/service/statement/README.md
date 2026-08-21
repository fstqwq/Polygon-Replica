# `app/service/statement`

Owns statement language discovery, source interpretation, FreeMarker rendering, TeX compilation, Pandoc HTML conversion, structured samples, and preview records.

Inputs are workspace or native package statement source, problem limits, and sample evidence. Outputs are rendered TeX trees, HTML fragments, PDFs, logs, and preview read models. Authored files remain source; generated trees and preview payloads are cache or native package content.

Problem and contest rendering share one sample model for pair, multi-pass, interactive, and multi-pass interactive examples. The optional authored `statement/examples.tex` overrides the canonical fallback and is rendered beside `problem.tex`.

The [problem source protocol](../../../../protocol/problem-source.md) owns authored layout. The [statement preview protocol](../../../../protocol/statement-preview.md) owns rendering, identity, sandbox, and cache behavior.
