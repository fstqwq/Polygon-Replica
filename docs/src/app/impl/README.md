# `app/impl`

Implementation packages translate HTTP inputs into service calls, enforce
request authorization, build HTML/JSON responses, and orchestrate user-facing
flows. Current areas are admin, agent, auth, contest, Judgehost, preview,
problem, root, runtime, run/export, test specification, and workspace.

Reusable domain behavior lives in `app/service`. Contest build policy and
verification planning/execution are service-owned. `app/impl/runtime/dependency.py`
is only the request-bound accessor for the exact `ApplicationRuntime` installed
on the FastAPI application; it does not construct services or own process
lifecycle.

The remaining reusable aggregation in `workspace/run_view_detail.py`,
`contest/problem_rows.py`, and `workspace/context_operation.py` is recorded
precisely in the
[findings ledger](../../../implementation/findings.md#placement-and-maintainability).
