# Configuration

## Bootstrap environment

Environment variables select resources required before SQLite configuration can
be read:

| Variable | Purpose | Default |
| --- | --- | --- |
| `POLYGON_REPLICA_DB` | SQLite database | `/var/lib/polygon-replica/metadata.db` |
| `POLYGON_REPLICA_BARE_ROOT` | bare Git repositories | `/srv/polygon-replica/git` |
| `POLYGON_REPLICA_WORKSPACE_ROOT` | user workspaces | `/srv/polygon-replica/workspaces` |
| `POLYGON_REPLICA_ARTIFACTS_ROOT` | derived packages and Contest outputs | `/srv/polygon-replica/export` |
| `POLYGON_REPLICA_CACHE_ROOT` | runtime/cache tree | `/tmp/polygon-replica` |
| `POLYGON_REPLICA_CONTEST_SOURCE_ROOT` | contest sources and attachments | `/var/lib/polygon-replica/contest-sources` |
| `POLYGON_REPLICA_BACKUP_ROOT` | source backup and operator archives | `/var/backups/polygon-replica` |

Root paths must obey the [storage protocol](../protocol/storage.md).

`POLYGON_REPLICA_ENCRYPTION_KEY` supplies the 32-byte base64url key used to
encrypt the SMTP password stored in SQLite. It is read directly from the process
environment, is not a `system_config` key, and must remain stable while the
encrypted password is retained.

Launcher-only variables are not application storage configuration:

| Launcher | Variables and current behavior |
| --- | --- |
| `scripts/start_local.sh` | `POLYGON_REPLICA_HOST` (default `127.0.0.1`), `POLYGON_REPLICA_PORT` (default `8000`), `POLYGON_REPLICA_DEV_RELOAD`, `POLYGON_REPLICA_TLS_KEY_PATH`, `POLYGON_REPLICA_TLS_CERT_PATH`, `POLYGON_REPLICA_UVICORN_GRACEFUL_TIMEOUT_SEC`, `POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC`, and `POLYGON_REPLICA_SHUTDOWN_TIMEOUT_SEC` |
| Docker entrypoint | `POLYGON_REPLICA_HOST` (default `0.0.0.0`), `POLYGON_REPLICA_PORT` (default `8001`), and `POLYGON_REPLICA_KEEPALIVE_TIMEOUT_SEC` |
| systemd unit | host `127.0.0.1`, port `8001`, keepalive `30` seconds, and proxy-header acceptance are fixed in the rendered unit |

`POLYGON_REPLICA_RUNTIME_USER` is installer input, not application runtime
configuration. The systemd installer stores bootstrap assignments in
`/etc/polygon-replica.env` as `root:root` mode `0600`. Rerunning it replaces the
installer-owned root and TLS-path values while retaining other valid,
uniquely-named single-line assignments such as
`POLYGON_REPLICA_ENCRYPTION_KEY`. The renderer accepts an optional shell
`export` prefix in an existing file but always writes systemd-compatible
`NAME=VALUE` records. It rejects unbalanced quotes and a trailing escape that
would turn the record into a multiline continuation.

## Durable application settings

Admin-managed settings are stored as JSON values in SQLite `system_config`.
The typed registry under `app/config/` is the only authority for every key's
type, default, range, category, description, and restart behavior. It currently
contains 95 settings, including `PROBLEM_ZIP_MAX_EXPANDED_BYTES`,
`CONTEST_MAX_PROBLEMS`, and `STATEMENT_SAMPLE_MAX_BYTES`. SQLite stores only
values that differ from registry defaults. Bootstrap environment variables
select resources needed before this registry can be loaded.

`app/main_constant.py` contains fixed protocol, path, regular-expression,
template, and enumeration values. It does not contain admin-editable defaults.
Fixed ZIP limits such as the per-problem 4096-entry ceiling and 4 MiB metadata
ceiling likewise do not appear in the registry.

At startup, every persisted override is normalized and the complete resulting
snapshot is validated. An unknown key, malformed value, invalid regular
expression, colliding cookie name, or inconsistent min/max pair prevents
startup and names the offending key rather than silently restoring a default.
The statement sample limit cannot exceed the whole `tests/spec.json` textarea
limit.

Secure-cookie behavior is controlled by the durable `AUTH_COOKIE_SECURE`
setting under `system_config` authority.

SMTP connection fields live in the singleton `smtp_config` row. Its password is
stored encrypted with the deployment key above.

## Change behavior

Configuration input is normalized at the admin boundary, combined with all
unchanged values, validated as one snapshot, and persisted. Live
reload atomically replaces one immutable `ConfigValues` snapshot; an operation
that needs several settings captures that snapshot once. Secrets are redacted
from status and application log output.

Most settings become active on that replacement. A registry definition marked
`restart_required` updates the persisted snapshot but remains pending in the
current process. A new process validates and activates it during startup. The
three cookie names are restart-required: after a rename, cookies under the old
name become invalid and browsers must authenticate again. Worker sizing,
problem expanded-ZIP budget, and contest problem admission limit follow the
same restart-only model.
