# `app/service/runtime`

Owns the small runtime-facing service boundary: SQLite metadata initialization,
startup cancellation of in-flight summary rows, and the effective compiler and
runner settings consumed by execution code. Inputs are the current runtime
configuration and startup reason/time; outputs are canonical toolchain values
and reconciliation warnings. It owns no independent persistent store.

Application-wide dependency construction occurs in the top-level
`app.runtime.ApplicationRuntime`. `app/runtime_lifecycle.py` receives that
object explicitly for startup and shutdown; this service package does not
locate the application or construct other services.
Restart behavior is owned by the
[execution](../../../../protocol/execution.md) and
[storage](../../../../protocol/storage.md) protocols.
