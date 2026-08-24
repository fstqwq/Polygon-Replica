# `app/service/workspace`

Owns safe operations on an already-resolved mutable checkout: relative-path
validation, bounded file list/read/write/upload/delete, snapshot ZIP creation,
archive comparison/application, and read/write mutation locking. It returns
typed file views, archive diffs, snapshot bytes, and refreshed workspace status.

Checkout files are this service's direct persistent state; workspace identity and status remain in SQLite. Snapshots are returned to the caller. Archive replacement validates input and uses recoverable mutation steps, though process interruption may require reapplying the archive. Published source remains the Git `main` commit.
