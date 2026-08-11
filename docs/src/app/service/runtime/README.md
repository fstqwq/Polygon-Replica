# `app/service/runtime`

Owns the small runtime-facing service boundary: SQLite metadata initialization,
startup cancellation of in-flight summary rows, and the effective compiler and
runner settings consumed by execution code. Inputs are the current runtime
configuration and startup reason/time; outputs are canonical toolchain values
and reconciliation warnings. It owns no independent persistent store.

Application-wide dependency construction occurs in `app/impl/runtime/config.py`
and lifecycle orchestration in `app/impl/runtime/lifecycle.py`.
Restart behavior is owned by the
[execution](../../../../protocol/execution.md) and
[storage](../../../../protocol/storage.md) protocols.
