# `app/service/problem`

Owns pure or workspace-local interpretation of authored problem source: ordered
build configuration, runtime-limit display, testcase specification parsing,
solution metadata, content review, and readiness projection. Its primary inputs
are workspace files and already-authorized problem/workspace metadata; its
outputs are canonical JSON/text writers and typed read models for orchestration
and UI code. `ProblemSourceQueryService` owns solution metadata, test editor,
and custom-run selector projections; selectors describe the current authored
Source and never infer a new run from disposable Verification cache files.

Git remains authoritative for committed source. This package does not own Git
publication, execution, or derived outputs. Readiness combines read-only Git,
verification, and package projections without collapsing their states. The
authored shapes and fallbacks are owned by the
[problem-source protocol](../../../../protocol/problem-source.md).

Verification readiness considers the records visible from the current
workspace: records owned by that workspace and problem-level records whose
`workspace_id` is `NULL`. A record is current only when its persisted commit or
source signature matches the current workspace content; record ownership is not
itself source equivalence.
