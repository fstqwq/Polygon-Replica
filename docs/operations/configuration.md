# Configuration

## Bootstrap environment

Environment variables select resources required before SQLite configuration can
be read:

| Variable | Purpose | Default |
| --- | --- | --- |
| `POLYGON_REPLICA_DB` | SQLite database | `/var/lib/polygon-replica/metadata.db` |
| `POLYGON_REPLICA_BARE_ROOT` | bare Git repositories | `/srv/polygon-replica/git` |
| `POLYGON_REPLICA_WORKSPACE_ROOT` | user workspaces | `/srv/polygon-replica/workspaces` |
| `POLYGON_REPLICA_ARTIFACTS_ROOT` | export and derived artifacts | `/srv/polygon-replica/export` |
| `POLYGON_REPLICA_CACHE_ROOT` | runtime/cache tree | `/tmp/polygon-replica` |
| `POLYGON_REPLICA_CONTEST_SOURCE_ROOT` | contest sources and attachments | `/var/lib/polygon-replica/contest-sources` |
| `POLYGON_REPLICA_BACKUP_ROOT` | operator archives | `/var/backups/polygon-replica` |

TLS paths, encryption-key material, bind addresses, and process launch settings
are also bootstrap concerns where used by the selected launcher. Root paths must
obey the [storage protocol](../protocol/storage.md).

## Durable application settings

Admin-managed settings are stored as JSON values in SQLite `system_config`.
`app/main_constant.py` declares their metadata/defaults and runtime application
updates the canonical constants consumed by services. The table stores one
current value per key; there is no generation/activation state machine.

Secure-cookie behavior comes from this durable configuration path. The removed
`POLYGON_REPLICA_AUTH_COOKIE_SECURE` environment variable was never an
application override and MUST NOT be added to deployment manifests.

SMTP connection fields live in the singleton `smtp_config` row. Its password is
stored encrypted; the encryption key is deployment bootstrap material and must
remain stable while encrypted values are retained.

## Change behavior

Configuration input is validated at the admin boundary, persisted, applied to
the runtime constants, and audited. Settings that affect process-local services
take effect according to their existing handlers; the system does not claim an
atomic whole-configuration generation switch. Secrets are redacted from status
and audit output.
