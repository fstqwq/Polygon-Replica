# `app/service/repository`

Owns Git mechanics and repository-backed identity/workspace coordination.
`GitService` accepts a validated workspace and Git operation and returns status,
history, diffs, or commit ids. `WorkspaceService` provisions bare repositories
and checkouts, resolves users/problems/access, records workspace status, and
provides per-workspace locking. Merge services compare a mutable checkout with
the published branch and apply or undo a selected result.

Committed source and history live in bare Git repositories; checkout contents
live in the workspace root; problem, user, ACL, workspace, and audit metadata
live in SQLite. Merge previews are process-local, while publication and
workspace state persist through Git and SQLite. File/archive-specific workspace
operations are separated into the sibling
[workspace service](../workspace/README.md). The source authority is defined by
the [problem-source protocol](../../../../protocol/problem-source.md).
