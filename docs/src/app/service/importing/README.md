# `app/service/importing`

Owns Polygon, Native Package, ICPC, and DOMjudge-compatible archive admission
and conversion into canonical workspace source, plus Polygon Contest archive
inspection. Uploads are file-backed; `ArchiveView` preflights central-directory
structure and accounts all entries while `ExpansionBudget` charges selected
members' declared and actual output. Child Contest views share the outer budget
and add a per-problem budget. Importers stream selected payloads into staging
rather than retaining whole archives or child packages as bytes.

Native Package import is identified by `config/problem.json`. It
selects only canonical authored roots, so `test-data/`, reproducible
`statement-build/`, and every other unknown top-level member remain unopened.
It stages and validates only authored source and moves only that source into the
workspace. Import never copies generated answers or verification provenance
into Git and never registers a Native Package for the target problem.
Polygon and ICPC importers likewise leave unknown external payloads unopened.

The calling implementation owns Git, SQLite, and integration of the staged
tree. Import converts external input into the current canonical workspace
shape; it does not retain a parallel package model. Formats, budgets, and merge
semantics are owned by the
[package protocol](../../../../protocol/package.md).
