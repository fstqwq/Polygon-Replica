# `app/service/repository`

Owns Git mechanics and repository-backed workspace coordination: repository and checkout provisioning, status, history, diffs, publication, merge comparison, and per-workspace locking.

Committed source and history live in bare Git repositories, checkout contents live in the workspace root, and identity metadata lives in SQLite. Merge previews are process-local. File and archive operations belong to the sibling
[workspace service](../workspace/README.md). The source authority is defined by
the [problem-source protocol](../../../../protocol/problem-source.md).

The bounded identity caches retain only database IDs. Each lookup reads the current
user or problem row and rejects stale ID/name pairs. Permissions and user flags
come from the current database row.

Workspace context resolves the current problem, user and checkout. Verification
history belongs to the verification read services.

`WorkspaceService.publish()` coordinates browser, Agent, package import and
Contest publication under the problem lock. `GitService` performs the push;
the workspace store broadcasts the resulting revision to existing list rows in
one database update. Workspace status refresh uses the same problem lock so a
stale read cannot overwrite a newer publication. The caller holds its workspace
lock before entering publication. Post-push metadata failures are logged for
repair without undoing the published commit. Activity timestamps and sync
semantics belong to the [problem-source protocol](../../../../protocol/problem-source.md).
