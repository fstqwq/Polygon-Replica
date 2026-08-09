# `app/service/workspace`

Owns mutable per-user checkout state, workspace lookup/status, replacement, and
source-file operations used by authoring and import. It coordinates repository
and disk services while SQLite stores workspace identity and projections.

Published source remains the Git `main` commit. Workspace replacement and
publish crash consistency are tracked as STO-008.
