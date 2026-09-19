# `app/impl`

Implementation packages translate HTTP inputs into authorized service calls and build HTML or JSON responses.

Reusable domain behavior, verification planning, and package policy remain in `app/service`. Implementation modules may coordinate use cases but do not recreate service rules.

Service-owned read models provide problem, contest, and verification state. HTTP modules authorize access and project them into page- or API-specific responses.

The authentication middleware checks browser sessions and same-origin state
changes in the thread pool for protected browser paths. Identity is shared by
the ASGI request scope; each new request checks durable session validity.
Logout and password rotation invalidate the current request's identity after
revocation. Public handlers can inspect identity when their own behavior needs it.
The middleware adds security and backend timing headers when the response starts
and forwards response body messages directly.

[Run and export handlers](run_export/README.md) own verification page, fragment,
sample download and export coordination.
