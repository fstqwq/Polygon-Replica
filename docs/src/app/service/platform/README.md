# `app/service/platform`

Contains shared mechanisms: worker queue, admission gate, maintenance, source
backup, filesystem/path helpers, Git/process execution, runtime blobs and cache
indexes, locking, crypto, configuration persistence, and workspace path safety.
Typed configuration definitions and the active snapshot live in `app/config/`;
platform configuration services validate and persist registry overrides.

Callers provide canonical jobs, paths, content identities, cache signatures, or
maintenance requests. The package returns bounded worker futures/snapshots,
validated paths, immutable runtime blob descriptors, cache entries, cleanup
reports, and the single published source archive. Domain services remain
responsible for interpreting results and publishing their own status
transitions.

`platform.fs.StorageLayout` is constructed once from environment-derived
settings. It validates configured root geometry and owns every derived locator;
services use it as their filesystem capability rather than reading settings to
assemble paths.

The worker queue, runtime cache index, blob locks, and maintenance state are
process-local. Maintenance has explicit owners: `admission` tracks the shared
gate and active requests; `coordinator` owns the one active operation and its
snapshot; `plan` declares cleanup-safe tables; `database` and `filesystem`
provide destructive mechanics; and the `artifact` maintenance module orders the cleanup stages
and runtime resets. The queue JSONL is diagnostic history, not a recoverable
job source, and startup resets it together with in-memory queue records.
Runtime blobs and cache entries live below the startup-cleared cache trees.
SQLite is used only where a mechanism persists configuration or cleanup
effects.
Source backup archives only the bare Git and workspace roots while the shared
maintenance gate is closed. It writes a temporary archive below the backup root
and atomically replaces the one downloadable latest archive; it does not copy
SQLite or other storage roots. Root ownership and maintenance ordering belong
to the [storage protocol](../../../../protocol/storage.md).
