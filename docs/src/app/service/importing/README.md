# `app/service/importing`

Owns Polygon, Polygon Replica, ICPC, and DOMjudge-compatible archive admission
and conversion into canonical workspace source, plus Polygon Contest archive
inspection. Uploads are file-backed; `ArchiveView` preflights central-directory
structure and accounts all entries while `ExpansionBudget` charges selected
members' declared and actual output. Child Contest views share the outer budget
and add a per-problem budget. Importers stream selected payloads into staging
rather than retaining whole archives or child packages as bytes.

Polygon Replica package import fully validates its verified test-data manifest
and payload inventory in isolated staging. It then discards all `test_data/`
content and moves only authored source into the workspace. Import never copies
generated answers into Git or registers a verified revision for the target
problem. Polygon and ICPC importers likewise leave unknown external payloads
unopened.

The calling implementation owns Git, SQLite, and integration of the staged
tree. Import converts external input into the current canonical workspace
shape; it does not retain a parallel package model. Formats, budgets, and merge
semantics are owned by the
[package protocol](../../../../protocol/package.md).
