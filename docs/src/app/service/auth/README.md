# `app/service/auth`

Owns user password-verifier records, registration, durable registration rate
limits, browser sessions, sudo sessions, and system-admin account changes. It
accepts already-normalized identity and verifier values and returns session
tokens or authenticated identities; cookie parsing and response handling remain
in `app/impl/auth`.

Initial setup atomically creates the trusted-email system administrator and saves the registration email policy in SQLite.

Users, pending registrations, rate-limit buckets, auth sessions, and sudo
sessions are stored in SQLite. Sessions expire or are revoked when access is
withdrawn. The [system trust boundary](../../../../design/system.md#trust-boundaries)
owns sudo's browser-session and non-transferability invariant. Cookie names,
lifetime, and secure behavior come from durable
[system configuration](../../../../operations/configuration.md).
