# `app`

`app/main.py` creates the FastAPI application and registers lifecycle and route
composition. `app/db.py` owns canonical SQLite DDL and connection helpers.
`app/config/` owns the complete typed admin-configuration registry and the
atomically replaceable active snapshot. `app/main_constant.py` contains only
fixed invariants. `app/main_util.py` contains shared boundary helpers.

Subpackages separate HTTP registration (`route`), request/use-case handling
(`impl`), reusable domain services (`service`), and rendered assets
(`static`/`template`). The service graph is constructed by the top-level
`app.runtime.ApplicationRuntime`; `app/runtime_lifecycle.py` owns startup and
shutdown orchestration.

The normal dependency direction is `route` to `impl` to `service`. Modules in
all three layers may use the small application support modules at `app/` where
their responsibility requires it. Services do not import route registration,
templates, static assets, runtime composition, or implementation modules. The
import checker discovers the complete `app` graph directly, and cycles have no
scope or exception list.
