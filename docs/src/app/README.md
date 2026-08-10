# `app`

`app/main.py` creates the FastAPI application and registers lifecycle and route
composition. `app/db.py` owns canonical SQLite DDL and connection helpers.
`app/config/` owns the complete typed admin-configuration registry and the
atomically replaceable active snapshot. `app/main_constant.py` contains only
fixed invariants. `app/main_util.py` contains shared boundary helpers.

Subpackages separate HTTP registration (`route`), request/use-case handling
(`impl`), reusable domain services (`service`), and rendered assets
(`static`/`template`). The service graph is constructed in
`app/impl/runtime/config.py`; startup and shutdown orchestration remains in
`app/impl/auth/internal/runtime.py`, the placement described by
[PLC-001](../../implementation/findings.md#placement-and-maintainability).

The normal dependency direction is `route` to `impl` to `service`. Modules in
all three layers may use the small application support modules at `app/` where
their responsibility requires it. Services do not import route registration,
templates, or static assets. The current service imports of runtime composition
and the workspace verification DAG are narrow placement exceptions recorded in
the findings ledger; the import boundary configuration names only those
modules, not `app.impl` as a generally allowed service dependency.
