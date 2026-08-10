# `app/service/problem`

Owns pure or workspace-local interpretation of authored problem source: ordered
build configuration, runtime-limit display, testcase specification parsing,
solution metadata, content review, and readiness projection. Its primary inputs
are workspace files and already-authorized problem/workspace metadata; its
outputs are canonical JSON/text writers and typed read models for orchestration
and UI code.

Git remains authoritative for committed source. This package does not own Git
publication, execution, or artifacts. Readiness combines read-only Git,
verification, and package projections without collapsing their states. The
authored shapes and fallbacks are owned by the
[problem-source protocol](../../../../protocol/problem-source.md).
