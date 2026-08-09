# `app/service/auth`

Owns password/session authentication, sudo-session verification, registration,
rate limiting, and access-policy helpers. Browser auth and sudo cookies are
independent from agent and Judgehost credentials.

Sudo is bound to the browser session that completed elevation and is not
transferable. Cookie security policy is loaded from durable system
configuration.
