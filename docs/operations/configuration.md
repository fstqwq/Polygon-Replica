# Configuration

## Bootstrap environment

Environment variables select resources required before SQLite configuration can
be read:

| Variable | Purpose | Default |
| --- | --- | --- |
| `POLYGON_REPLICA_DB` | SQLite database | `/var/lib/polygon-replica/metadata.db` |
| `POLYGON_REPLICA_BARE_ROOT` | bare Git repositories | `/srv/polygon-replica/git` |
| `POLYGON_REPLICA_WORKSPACE_ROOT` | user workspaces | `/srv/polygon-replica/workspaces` |
| `POLYGON_REPLICA_ARTIFACTS_ROOT` | derived packages and contest outputs | `/srv/polygon-replica/export` |
| `POLYGON_REPLICA_CACHE_ROOT` | runtime/cache tree | `/tmp/polygon-replica` |
| `POLYGON_REPLICA_CONTEST_SOURCE_ROOT` | contest sources and attachments | `/var/lib/polygon-replica/contest-sources` |
| `POLYGON_REPLICA_BACKUP_ROOT` | source backup and operator archives | `/var/backups/polygon-replica` |

Root paths must obey the [storage protocol](../protocol/storage.md).

`POLYGON_REPLICA_ENCRYPTION_KEY` supplies the 32-byte base64url key used to encrypt the SMTP password stored in SQLite. It must remain stable while that password is retained.

Launcher-only variables are not application storage configuration:

| Launcher | Variables and current behavior |
| --- | --- |
| `scripts/start_local.sh` | `POLYGON_REPLICA_HOST` (default `127.0.0.1`), `POLYGON_REPLICA_PORT` (default `8000`), `POLYGON_REPLICA_DEV_RELOAD`, `POLYGON_REPLICA_TLS_KEY_PATH`, `POLYGON_REPLICA_TLS_CERT_PATH`, `POLYGON_REPLICA_UVICORN_GRACEFUL_TIMEOUT_SEC`, `POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC`, and `POLYGON_REPLICA_SHUTDOWN_TIMEOUT_SEC` |
| Docker entrypoint | `POLYGON_REPLICA_HOST` (default `0.0.0.0`), `POLYGON_REPLICA_PORT` (default `8001`), and `POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC` |
| systemd unit | host `127.0.0.1`, port `8001`, keepalive `30` seconds, and proxy-header acceptance are fixed in the rendered unit |

`POLYGON_REPLICA_RUNTIME_USER` selects the non-root account used by the systemd installer. The installer stores bootstrap assignments in `/etc/polygon-replica.env` as `root:root` mode `0600` and preserves valid operator-managed assignments when rerun.

## Durable application settings

Admin-managed settings are stored as JSON values in SQLite `system_config`. The typed registry under `app/config/` defines every key, type, default, range, category, description, and restart behavior. SQLite stores only values that differ from defaults.

Initial setup can set `AUTH_EMAIL_ALLOW_REGEX`. The selected value and the first trusted-email administrator are committed together, and the setting becomes active before the administrator session is issued.

At startup, persisted overrides are normalized and validated as one snapshot. An unknown key, malformed value, invalid regular expression, colliding cookie name, or inconsistent limit prevents startup and names the offending key.

Secure-cookie behavior is controlled by the durable `AUTH_COOKIE_SECURE`
setting under `system_config` authority.

SMTP connection fields live in the singleton `smtp_config` row. Its password is
stored encrypted with the deployment key above.

## Change behavior

Configuration input is normalized, combined with unchanged values, validated as one snapshot, and persisted. Secrets are redacted from status and application logs.

Most settings become active immediately. A setting marked `restart_required` is persisted but remains pending until the next process starts. Renaming a cookie invalidates cookies under the previous name and requires users to authenticate again.
