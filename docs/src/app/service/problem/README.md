# `app/service/problem`

Owns pure or workspace-local interpretation of authored problem source: ordered
build configuration, runtime-limit display, testcase specification parsing,
solution metadata, content review, and readiness projection. Its primary inputs
are workspace files and already-authorized problem/workspace metadata; its
outputs are canonical JSON/text writers and typed read models for orchestration
and UI code. `ProblemSourceQueryService` owns solution metadata, test editor,
and custom-run selector projections; selectors describe the current authored
Source and never infer a new run from disposable Verification cache files.
Strict codecs and the source-tree loader protect work entrances such as
Verification and Export. The authoring-source inspector instead keeps pages
editable, reports malformed or incomplete source, and performs only
unambiguous workspace normalization; consumers never receive its display
fallbacks.

Git remains authoritative for committed source. This package does not own Git
publication, execution, or derived outputs. Readiness combines read-only Git,
Verification, Native Package, and external-package status without collapsing
their states. The
authored shapes and fallbacks are owned by the
[problem-source protocol](../../../../protocol/problem-source.md).

Verification readiness considers the records visible from the current
workspace: records owned by that workspace and problem-level records whose
`workspace_id` is `NULL`. A record is current only when its persisted commit or
source signature matches the current workspace content; record ownership is not
itself source equivalence.

Problem pages consume one request-scoped shell projection. Authored metadata,
component state, content review, Workspace/Verification/Package readiness,
navigation labels, and workspace changes are derived once and then reused by
the page header, section navigation, sidebar, and page-specific handlers. The
Contest problem overview uses the same typed metadata, content-review, and
readiness models in its batched row projection. Templates do not rebuild those
states from raw Git, SQLite, or package records, and handlers do not retain a
parallel set of legacy convenience fields.
