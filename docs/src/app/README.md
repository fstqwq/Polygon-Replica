# `app`

The root package owns application creation, runtime composition, canonical SQLite DDL, typed configuration, fixed invariants, and shared boundary helpers.

Subpackages separate HTTP registration (`route`), request orchestration (`impl`), reusable domain behavior (`service`), and rendered assets (`static` and `template`).

Dependencies flow from `route` to `impl` to `service`. Services do not import routes, templates, static assets, runtime composition, or implementation modules. The import checker rejects cycles across the complete `app` graph.
