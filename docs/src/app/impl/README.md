# `app/impl`

Implementation packages translate HTTP inputs into authorized service calls and build HTML or JSON responses.

Reusable domain behavior, verification planning, and package policy remain in `app/service`. Implementation modules may coordinate use cases but do not recreate service rules.

Service-owned read models provide problem, contest, and verification state. HTTP modules authorize access and project them into page- or API-specific responses.

Problem pages retain their typed authoring configuration for the current
operation. The Tests editor shares those testcase entries; Run New reuses them
and the already-built solution component rows. Each form
keeps its own display limits, and submitted work goes through its service-owned
source checks.

Workspace verification projections share typed columns, testcase cells, pass
details, sanity checks and artifact previews with browser fragments and Agent
YAML. Historical execution mode and result facts come from the persisted
verification. Display limits and diagnostic formatting are applied by the
projection that owns the rendered fields.

The authentication middleware checks browser sessions and same-origin state
changes in the thread pool for protected browser paths. Identity is shared by
the ASGI request scope; each new request checks durable session validity.
Logout and password rotation invalidate the current request's identity after
revocation. Public handlers can inspect identity when their own behavior needs it.
The middleware adds security and backend timing headers when the response starts
and forwards response body messages directly.

Full pages build one public judgehost status for both the footer and its details
dialog. JSON redirect handlers apply Contest scope to the URL before encoding
the response; scoped route wrappers only adjust HTTP redirect headers.

[Run and export handlers](run_export/README.md) own verification page, fragment,
sample download and export coordination.
