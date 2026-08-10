# `app/service/workspace`

Owns safe operations on an already-resolved mutable checkout: relative-path
validation, bounded file list/read/write/upload/delete, snapshot ZIP creation,
archive comparison/application, and read/write mutation locking. It returns
typed file views, archive diffs, snapshot bytes, and refreshed workspace status.

Checkout files are the package's only direct persistent state; workspace
identity/status remain in SQLite through `repository.WorkspaceService`.
Snapshots are returned to the caller rather than retained here. Archive input
is validated before replacement, and write-locked operations refresh the status
projection after mutation. Repository merge replacement uses a recoverable
filesystem journal; archive application uses an in-process rollback copy, whose
interruption risk is tracked as
[STO-008](../../../../implementation/findings.md#storage-and-persistence).
Published source remains the Git `main` commit.
