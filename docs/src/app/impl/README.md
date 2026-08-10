# `app/impl`

Implementation packages translate HTTP inputs into service calls, enforce
request authorization, build HTML/JSON responses, and orchestrate user-facing
flows. Current areas are admin, agent, auth, contest, Judgehost, preview,
problem, root, runtime, run/export, test specification, and workspace.

Reusable domain behavior generally lives in `app/service`. Current large read
models, contest build policy, verification planning adapters, and runtime
composition that remain here are recorded in the
[findings ledger](../../../implementation/findings.md#placement-and-maintainability).
