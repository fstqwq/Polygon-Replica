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
The typed registry under `app/config/` is the only authority for every key's
type, default, range, category, description, and restart behavior. It currently
contains the 92 existing settings plus `PROBLEM_ZIP_MAX_EXPANDED_BYTES` and
`CONTEST_MAX_PROBLEMS`. SQLite stores only values that differ from registry
defaults; there is no second legacy schema or environment-variable fallback.

`app/main_constant.py` contains fixed protocol, path, regular-expression,
template, and enumeration values. It does not contain admin-editable defaults.
Fixed ZIP limits such as the per-problem 4096-entry ceiling and 4 MiB metadata
ceiling likewise do not appear in the registry.

At startup, every persisted override is normalized and the complete resulting
snapshot is validated. An unknown key, malformed value, invalid regular
expression, colliding cookie name, or inconsistent min/max pair prevents
startup and names the offending key rather than silently restoring a default.

Secure-cookie behavior comes from this durable configuration path. The removed
`POLYGON_REPLICA_AUTH_COOKIE_SECURE` environment variable was never an
application override and MUST NOT be added to deployment manifests.

SMTP connection fields live in the singleton `smtp_config` row. Its password is
stored encrypted; the encryption key is deployment bootstrap material and must
remain stable while encrypted values are retained.

## Change behavior

Configuration input is normalized at the admin boundary, combined with all
unchanged values, validated as one snapshot, persisted, and audited. Live
reload atomically replaces one immutable `ConfigValues` snapshot; an operation
that needs several settings captures that snapshot once. Secrets are redacted
from status and audit output.

Most settings become active on that replacement. A registry definition marked
`restart_required` updates the persisted snapshot but remains pending in the
current process. A new process validates and activates it during startup. The
three cookie names are restart-required: after a rename, cookies under the old
name are no longer read and browsers must authenticate again. Worker sizing,
problem expanded-ZIP budget, and contest problem admission limit follow the
same restart-only model.
