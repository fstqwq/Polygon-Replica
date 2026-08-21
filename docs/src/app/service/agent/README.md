# `app/service/agent`

Owns Agent registration codes, desktop identities and sessions, general
permissions, and expiring per-problem grants. A connected Agent authenticates
with a random `polygon_agent_...` bearer credential; SQLite stores only its
SHA-256 verifier. `session_id` identifies the session. `identity_hash` validates
registration and reconnect metadata and is not an authentication secret. The
service combines general scope with active per-problem grants, then caps that
declared scope by the connected user's current effective Problem role. Browser
sudo is never transferred to an Agent.

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
