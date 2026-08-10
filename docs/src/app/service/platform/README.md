# `app/service/platform`

Contains shared mechanisms: worker queue, admission gate, maintenance,
filesystem/path helpers, Git/process execution, runtime blobs and cache indexes,
locking, crypto, configuration persistence, and workspace path safety. Typed
configuration definitions and the active snapshot live in `app/config/`;
platform configuration services validate and persist registry overrides.

Callers provide canonical jobs, paths, content identities, cache signatures, or
maintenance requests. The package returns bounded worker futures/snapshots,
validated paths, immutable runtime blob descriptors, cache entries, and cleanup
reports. Domain services remain responsible for interpreting results and
publishing their own status transitions.

The worker queue, runtime cache index, blob locks, and maintenance state are
process-local. The queue JSONL is diagnostic history, not a recoverable job
source, and startup resets it together with in-memory queue records. Runtime
blobs and cache entries live below the startup-cleared cache trees. SQLite is
used only where a mechanism persists configuration, audit, or cleanup effects.
Root ownership and cleanup ordering belong to the
[storage protocol](../../../../protocol/storage.md).
