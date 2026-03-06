# DB_WRITE

Last updated: 2026-03-06

This note tracks major SQLite write-contention risks only.

## Current Write Model

- Metadata DB is SQLite (WAL enabled).
- SQLite still allows one writer at a time.
- Current retry knobs are in `app/db.py`.

## Major Risks

1. Manual transaction paths bypass shared retry wrapper in several hot handlers.
2. Some read-looking page paths can still trigger writes (workspace/status refresh side effects).
3. Preview/history and run summary paths can create write hotspots under concurrent access.
4. Long transaction sections in cancel/admin flows can extend lock hold times.

## Mitigation Direction

- Reduce write side effects in GET paths.
- Keep transactions short and narrow.
- Prefer idempotent retry-safe write helpers.
- Add targeted indices for high-frequency where-clauses.

For implementation tasks, use `BACKEND_TODO.md`.
