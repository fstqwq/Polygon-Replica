# `app/service/runtime`

Owns runtime metadata initialization and startup reconciliation of interrupted summary rows. It records startup metadata and returns reconciliation warnings without owning a separate persistent store.

Typed execution settings belong to `app/config`, toolchain telemetry belongs to judgehost, and application-wide construction belongs to the composition root.
Restart behavior is owned by the
[execution](../../../../protocol/execution.md) and
[storage](../../../../protocol/storage.md) protocols.
