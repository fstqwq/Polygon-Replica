# `app/impl`

Implementation packages translate HTTP inputs into service calls, enforce
request authorization, build HTML/JSON responses, and orchestrate user-facing
flows. Current areas are admin, agent, auth, contest, Judgehost, preview,
problem, root, runtime, run/export, test specification, and workspace.

Domain state and reusable policy should live in `app/service`. Known cases where
large read models, contest build policy, verification coordination, or runtime
composition remain here are recorded in the findings ledger rather than hidden
by a proposed directory structure.
