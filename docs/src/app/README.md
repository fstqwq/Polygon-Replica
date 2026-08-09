# `app`

`app/main.py` creates the FastAPI application and registers lifecycle and route
composition. `app/db.py` owns canonical SQLite DDL and connection helpers.
`app/main_constant.py` declares configuration metadata and current runtime
values. `app/main_util.py` contains shared boundary helpers.

Subpackages separate HTTP registration (`route`), request/use-case handling
(`impl`), reusable domain services (`service`), and rendered assets
(`static`/`template`). Runtime wiring currently lives under the auth
implementation package; that placement is tracked as PLC-001.
