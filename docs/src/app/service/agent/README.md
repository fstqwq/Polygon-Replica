# `app/service/agent`

Owns agent registration codes, desktop identities and sessions, problem access
requests, scoped tokens, and token authentication. Its inputs are a registered
agent identity, a user-approved problem and scope, and the current repository
ACL. It returns session/access status or a canonical token identity for HTTP
handlers.

Registration codes, sessions, requests, and tokens are durable in the
`agent_*` tables. Tokens expire or can be revoked; disconnecting a session
revokes its access. Effective scope is the lesser of the token's
`readonly`/`workspace`/`commit` grant and the user's current repository role.

HTTP transport remains under `app/impl/agent` and `app/route/agent_route.py`;
credential separation and the no-sudo invariant are owned by the
[system trust boundary](../../../../design/system.md#trust-boundaries), and
table ownership by the
[persistence protocol](../../../../protocol/persistence.md).
