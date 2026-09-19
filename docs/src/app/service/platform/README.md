# `app/service/platform`

Contains shared infrastructure mechanisms: worker queue, admission and maintenance gates, validated storage paths, Git and process execution, runtime blobs and cache indexes, locking, crypto, configuration persistence, cleanup, and source backup.

Domain services supply canonical jobs, paths, identities, and maintenance requests. Platform services return bounded worker state, validated locators, runtime blob descriptors, cache entries, and maintenance results without interpreting domain outcomes.

The runtime blob store reuses descriptors of content-addressed files during its
process lifetime. Reuse checks the current file type and size; missing files
remain unavailable until republished. Clearing the runtime store also clears
its descriptor cache.

A cache lookup can share a short-lived blob lookup between its entry and artifact
checks. It resolves each reference once while checking every expected file size.
The domain caller discards this lookup when the operation returns.

Worker state, cache indexes, locks, and admission state are process-local. Runtime blobs are content-addressed files below the cache root. Startup resets both. Source backup and generated-data cleanup run under the exclusive maintenance gate.

The [storage protocol](../../../../protocol/storage.md) defines roots, cleanup, backup contents, and recovery boundaries.
