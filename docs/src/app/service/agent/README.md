# `app/service/agent`

Owns agent registration, sessions, access requests, tokens, and agent-facing
authorization state. Agent tokens are a separate credential class from browser
sessions. They cannot acquire, inherit, or present browser sudo authority.

Durable state is stored in the `agent_*` tables. HTTP transport remains under
`app/impl/agent` and `app/route/agent_route.py`.
