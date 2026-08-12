# `app/service/importing`

Owns Native, Polygon, and ICPC archive admission and conversion into canonical
workspace source, plus Polygon Contest archive inspection. Uploads are
file-backed; `ArchiveView` preflights central-directory structure and accounts
all entries while `ExpansionBudget` charges only selected members' declared and
actual output. Child Contest views share the outer budget and add a per-problem
budget. Importers stream selected payloads into staging rather than retaining
whole archives or child packages as bytes.

Native `test_data/**` is deliberately unselected because it is materialization,
not source. Polygon and ICPC importers likewise leave unknown external payload
unopened.

The calling implementation owns Git, SQLite, and integration of the staged
tree. Import converts external input into canonical workspace source. Formats
and merge semantics are owned by the
[package protocol](../../../../protocol/package.md).
