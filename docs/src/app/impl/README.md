# `app/impl`

Implementation packages translate HTTP inputs into authorized service calls and build HTML or JSON responses.

Reusable domain behavior, verification planning, and package policy remain in `app/service`. Implementation modules may coordinate use cases but do not recreate service rules.

Service-owned read models provide problem, contest, and verification state. HTTP modules authorize access and project them into page- or API-specific responses.
