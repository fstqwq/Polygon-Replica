# `app/service/runtime`

Owns runtime metadata initialization and startup reconciliation of interrupted
summary rows through `RuntimeStateService`. It receives the configured SQLite
store and startup reason/time, then records metadata and returns reconciliation
warnings. It owns no independent persistent store.

Typed compiler and runner settings belong to `app/config`; Judgehost toolchain
telemetry belongs to the Judgehost service. Application-wide dependency
construction occurs in the top-level `app.runtime.ApplicationRuntime`.
`app/runtime_lifecycle.py` receives that object explicitly for startup and
shutdown; this service package does not locate the application or construct
other services.
Restart behavior is owned by the
[execution](../../../../protocol/execution.md) and
[storage](../../../../protocol/storage.md) protocols.
