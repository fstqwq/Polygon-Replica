# `app/service/disk`

This package contains SQLite store adapters for authentication, workspaces,
contests, previews, verifications, exports, runtime reconciliation, system
configuration, and SMTP configuration. Despite its package name, it does not
own the artifact/cache filesystem layout.

The stores accept canonical service identifiers and typed row values, execute
queries or transactions through `app.db.DB`, and return typed records or decoded
JSON projections. Filesystem locators are persisted as data for their owning
domain service; locator validation and file lifecycle remain outside this
package. The table and locator contracts are documented in the
[persistence](../../../../protocol/persistence.md) and
[storage](../../../../protocol/storage.md) protocols.

Store instances are process-lived wrappers around short-lived SQLite
connections and retain no task lifecycle of their own. Cross-store boundary
fragmentation is recorded as PLC-009.

Concrete historical table reconstructions run before these stores are used.
The dependency-light SQLite shape-upgrade owner invalidates old derived export
rows when removing their obsolete option identity and preserves historical job
rows without stale artifact references.
