# `app/route`

The route package registers FastAPI paths and methods for admin, agent, contest,
judgehost, maintenance, preview, problem, authentication, run/export, and test
workflows. It delegates handlers to `app/impl` and contains no durable domain
state.

The external `/api/v4/*` surface is defined by the [judgehost protocol](../../../protocol/judgehost.md). Problem-scoped routers assemble authorization and URL scope without owning problem behavior.
