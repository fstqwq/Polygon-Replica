# Application package map

This tree mirrors the current `app/` package. It explains responsibility and
dependency direction; protocol details remain in [docs/protocol](../protocol/README.md).

- [`app/`](app/README.md): application entry points and package boundaries
- [`app/impl`](app/impl/README.md): HTTP-facing use-case orchestration
- [`app/route`](app/route/README.md): FastAPI route registration
- [`app/service`](app/service/README.md): domain and platform services
- [`app/static`](app/static/README.md): browser assets
- [`app/template`](app/template/README.md): server-rendered HTML templates

The map follows the implementation. `app/` is not treated as a temporary
mapping to a proposed package tree.
