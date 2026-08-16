# `app/service/agent`

Owns agent registration codes, desktop identities and sessions, general
permissions, and expiring per-problem grants. A connected agent authenticates
with its session ID and identity hash. The service combines the session's
general permission with all active grants for the target problem, then caps the
declared scope by the connected user's current effective problem role.

Registration codes, sessions, approval requests, and grants are durable in the
`agent_*` tables. General permission has no expiry. Each problem approval adds
an independent `readonly`, `workspace`, or `commit` grant with its own expiry
or explicit forever lifetime. Disconnecting deletes the session and all of its
requests and grants. No ordinary Agent API uses a per-problem bearer token.

Contest roster discovery requires general `readonly` permission and current
Contest read access. It returns only a SQLite roster snapshot. The per-problem
Contest snapshot route rechecks the roster generation before provisioning a
workspace; Contest identity is not persisted as an authorization credential.

HTTP transport remains under `app/impl/agent` and `app/route/agent_route.py`;
credential separation and the no-sudo invariant are owned by the
[system trust boundary](../../../../design/system.md#trust-boundaries), and
table ownership by the
[persistence protocol](../../../../protocol/persistence.md).
