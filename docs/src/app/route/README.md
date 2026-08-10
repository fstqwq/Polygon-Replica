# `app/route`

The route package registers FastAPI paths and methods for admin, agent, contest,
Judgehost, maintenance, preview, problem, authentication, run/export, and test
workflows. It delegates handlers to `app/impl` and contains no durable domain
state.

`judgehost_route.py` registers the external `/api/v4/*` surface; its exact wire
contract is in the [Judgehost protocol](../../../protocol/judgehost.md).
Problem-scoped routers assemble authorization and URL scope without owning
problem behavior.
