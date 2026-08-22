# `app/service/disk`

This package contains SQLite store adapters for authentication, workspaces,
contests, statement previews, verifications, exports, runtime reconciliation, system
configuration, and SMTP configuration. Despite its package name, it does not
own the derived/cache filesystem layout.

Stores accept canonical identifiers and typed values and return typed records or decoded JSON projections. Filesystem locator validation and lifecycle remain with the owning domain service. The table and locator contracts are documented in the
[persistence](../../../../protocol/persistence.md) and
[storage](../../../../protocol/storage.md) protocols.

Stores retain no domain task lifecycle. A coordinating service owns workflows spanning multiple stores.

Existing databases are validated read-only before use. Missing required objects block runtime; extra schema objects are tolerated.
