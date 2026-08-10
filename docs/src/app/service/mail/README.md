# `app/service/mail`

Owns validation and use of SMTP configuration plus outgoing registration and
test messages. It accepts admin form fields or a normalized recipient/message
request and returns a redacted configuration snapshot or delivery success.

Host, port, username, and encrypted password live in the singleton
`smtp_config` row. Reading or changing the password requires the stable
deployment encryption key described in
[configuration](../../../../operations/configuration.md). Snapshot and audit
payloads expose only whether the password changed or is configured, never its
plaintext or ciphertext. Delivery has no durable application queue: each send
completes in its calling request.
