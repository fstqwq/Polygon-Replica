# `app/impl`

Implementation packages translate HTTP inputs into service calls, enforce
request authorization, build HTML/JSON responses, and orchestrate user-facing
flows. Current areas are admin, agent, auth, contest, judgehost, preview,
problem, root, runtime, run/export, test specification, and workspace.

Reusable domain behavior lives in `app/service`. Contest package-download policy
and verification planning/execution are service-owned. `app/impl/runtime/dependency.py`
is only the request-bound accessor for the exact `ApplicationRuntime` installed
on the FastAPI application; it does not construct services or own process
lifecycle.

Problem-source, Contest problem, and Verification detail aggregation are
service-owned read models. HTTP modules authorize requests and project those
models into page- or API-specific responses.
