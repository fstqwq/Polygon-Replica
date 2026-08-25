# Operator deployment runbook

This runbook covers the two supported single-process layouts: a Debian/Ubuntu
host with systemd, or the checked-in Docker Compose service. Substitute values
shown as `<...>`. Keep runtime data outside the checkout.

## Before installation

Prepare a DNS name, ports 80/443, and one non-root service account. The examples
use `polygon` and `/opt/polygon-replica`. A host installation must already
provide the regular GIL-enabled `python3.14` interpreter:

```bash
sudo useradd --create-home --shell /bin/bash polygon
sudo install -d -o polygon -g polygon /opt/polygon-replica
sudo -u polygon git clone <repo-url> /opt/polygon-replica
```

The web process listens on loopback port 8001. Only nginx is public. Run exactly
one application process or Compose replica.

## Systemd installation

Run the installer as root while explicitly selecting the non-root runtime
account:

```bash
cd /opt/polygon-replica
sudo POLYGON_REPLICA_RUNTIME_USER=polygon ./scripts/install_host.sh
```

The installer prepares dependencies, storage, user namespaces, bubblewrap, TeX, `.venv`, `/etc/polygon-replica.env`, and the systemd unit. The application runs as the selected non-root account. The environment file is owned by root with mode `0600`; rerunning the installer preserves valid operator-managed assignments.

Inspect the result:

```bash
sudo systemctl status polygon-replica.service
sudo systemctl cat polygon-replica.service
sudo journalctl -u polygon-replica.service -n 200 --no-pager
curl -I http://127.0.0.1:8001/
```

Generate and retain a stable encryption key if SMTP credentials will be stored:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

Add `POLYGON_REPLICA_ENCRYPTION_KEY=<key>` and restart:

```bash
sudoedit /etc/polygon-replica.env
sudo systemctl restart polygon-replica.service
```

Losing or changing the key makes the stored SMTP password unreadable.

## Docker Compose installation

Enable host user namespaces before starting the container:

```bash
sudo tee /etc/sysctl.d/99-polygon-replica-sandbox.conf >/dev/null <<'EOF'
kernel.unprivileged_userns_clone = 1
user.max_user_namespaces = 1048576
EOF
if test -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns; then
  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | \
    sudo tee -a /etc/sysctl.d/99-polygon-replica-sandbox.conf
fi
sudo sysctl --system
```

Create the permanent backup bind and start Compose. For a new deployment:

```bash
sudo install -d -o 1000 -g 1000 -m 0700 /var/backups/polygon-replica
umask 077
printf '%s\n' \
  'POLYGON_REPLICA_BACKUP_HOST_DIR=/var/backups/polygon-replica' >.env
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs --tail=200 app
curl -I http://127.0.0.1:8001/
```

If SMTP is used, add the stable encryption key to `.env` and explicitly pass
`POLYGON_REPLICA_ENCRYPTION_KEY` through the Compose `environment` block. Keep
`.env` mode `0600`.

The root `.env` is client-side Compose configuration and may contain deployment
secrets. `.dockerignore` excludes `.env`, `.env.*`, and their nested equivalents
from the Docker build context. These exclusions MUST remain in place for local,
shared, and remote BuildKit builders. Selective `COPY` instructions only keep a
file out of the final image; they do not keep it out of the context sent to the
builder.

## TLS proxy

A TLS certificate, including a locally trusted self-signed certificate, is strongly recommended. Outside localhost, the browser password flow requires HTTPS because it uses Web Crypto. `AUTH_COOKIE_SECURE` also defaults to `true`, so authentication cookies are sent only over HTTPS.

Configure nginx or another TLS proxy with the deployment's certificate. The
nginx server block requires:

```nginx
server {
    listen 443 ssl;
    server_name <domain>;
    ssl_certificate <certificate-path>;
    ssl_certificate_key <private-key-path>;
    client_max_body_size 1024m;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
```

Keep port 8001 private because uvicorn trusts forwarded headers from its direct
peer. A same-host judgehost may use the private HTTP listener; remote judgehosts
must use the HTTPS endpoint.

## First use

Open `https://<domain>/` and complete initial setup. Setup creates the administrator, saves the public-registration email regex, and displays effective storage paths.

## Judgehost

In Settings configure and save:

```text
JUDGEHOST_ENABLE = true
JUDGEHOST_API_USERNAME = judgehost
JUDGEHOST_API_TOKEN = <strong random secret>
```

Use the Settings-generated judgehost command. Each daemon needs a unique hostname, daemon ID, CPU assignment, and unused `RUN_USER_UID_GID`. Verify IDs with `getent passwd <id>` and `getent group <id>`. A same-host container normally reaches `http://host.docker.internal:8001/`.

### Judgehost image choice

The generated command uses `domjudge/judgehost:latest`.

For long-lived judgehosts, the recommended source-built image is
[`fstqwq/domjudge`](https://github.com/fstqwq/domjudge) at commit
`6eb5e99d352c4f1ef70f540378b6cf069abef6be`. It tracks the upstream 10.0
development line and adds:

- [`b52a97af`](https://github.com/fstqwq/domjudge/commit/b52a97af01f96cbe39267a917503393c548e9701)
  resets a testcase result directory before a retry, removing artifacts left by
  an interrupted attempt.
- [`6eb5e99d`](https://github.com/fstqwq/domjudge/commit/6eb5e99d352c4f1ef70f540378b6cf069abef6be)
  reclaims the downloaded testcase cache when ordinary judging-directory
  cleanup cannot restore the configured free-space threshold.

The fork includes upstream 10.0 development-line throughput improvements for workloads with many short testcases.

Build the pinned source with DOMjudge's packaging repository on a Linux Docker
host. The build starts a temporary privileged container:

```bash
build_root="$(mktemp -d)"
git clone https://github.com/fstqwq/domjudge.git \
  "$build_root/domjudge"
git -C "$build_root/domjudge" checkout \
  6eb5e99d352c4f1ef70f540378b6cf069abef6be

git clone https://github.com/DOMjudge/domjudge-packaging.git \
  "$build_root/domjudge-packaging"
git -C "$build_root/domjudge-packaging" checkout \
  215feabbf24c04a7ff27a3d1962db1360454bcfd

git -C "$build_root/domjudge" archive \
  --format=tar.gz \
  --prefix=domjudge-fstqwq/ \
  --output="$build_root/domjudge-packaging/docker/domjudge.tar.gz" \
  HEAD

cd "$build_root/domjudge-packaging/docker"
./build-judgehost.sh polygon-judgehost:10.0-dev-6eb5e99
```

Keep the command generated by Settings and replace its final image reference
with `polygon-judgehost:10.0-dev-6eb5e99`.

## SMTP (optional)

SMTP is optional. Before it is enabled, Polygon Replica sends no email, and public registration creates accounts without an email-verification step.

SMTP host, port, username, and password are configured in **Admin -> Mail** and stored in SQLite. They are not environment variables. The deployment environment supplies only `POLYGON_REPLICA_ENCRYPTION_KEY`, which encrypts the SMTP password at rest.

Configure the encryption key before entering an SMTP password. Generate one stable 32-byte base64url value with the command in [Systemd installation](#systemd-installation). For systemd, save it in `/etc/polygon-replica.env` and restart the service. For Docker Compose, save it in `.env` and pass it into the container explicitly:

```yaml
services:
  app:
    environment:
      POLYGON_REPLICA_ENCRYPTION_KEY: ${POLYGON_REPLICA_ENCRYPTION_KEY:?set POLYGON_REPLICA_ENCRYPTION_KEY}
```

Recreate the container after changing its environment:

```bash
sudo docker compose up -d
```

Keep the same key for the lifetime of the stored SMTP password. If the key is lost or replaced, open **Admin -> Mail** and enter the SMTP password again under the new key.

In **Admin -> Mail**, enter the provider's SMTP hostname, port, login username, and password or application password. The application always authenticates with that username and password. Transport security is selected from the port:

| Port | Connection |
| --- | --- |
| `465` | SMTP over implicit TLS |
| `587` | SMTP followed by STARTTLS |
| Any other port | Plain SMTP |

The sender address is the SMTP username when it contains `@`; otherwise it is `polygon-replica@localhost`. Providers that restrict sender addresses should therefore use the mailbox address as the username. After saving, use **Send test email** on the same page and confirm that the message arrives.

Once host, username, and password are configured, public registration sends a verification code by email.

## Upgrade

Create and download a source backup from Admin first. Then update one deployment
at a time.

When the target revision tightens the canonical problem-source rules, audit the
currently published revisions before deployment:

```bash
PYTHONPATH=. .venv/bin/python scripts/check_problem_sources.py \
  --db <sqlite> \
  --bare-root <repos>
```

The command opens SQLite read-only, extracts each published `main` revision to a
temporary directory, and reports source validation errors. It does not change
SQLite, Git repositories, or workspaces. Run it again with the target revision's
code and Python environment when diagnosing a published revision rejected after
an upgrade.

Application startup does not alter an existing SQLite schema. Before installing a revision with schema changes, compare `app/db.py` at the deployed and target commits. Stop the application and judgehosts, back up SQLite with its WAL/SHM files, and apply the complete diff offline. Preserve IDs and relationships; require `foreign_key_check`, `integrity_check`, and application schema admission to pass before reopening traffic.

The latest breaking database change is `Retire legacy workspace preview
compile`, which removes the disposable legacy `previews` table. Deployments
whose schema still contains that table must stop the application and
judgehosts, back up the SQLite database together with its WAL and SHM files,
and apply the following offline change in addition to every other schema diff
between the deployed and target revisions:

```sql
DELETE FROM system_config
WHERE key = 'PREVIEW_LOG_REF_LIST_LIMIT';

DROP TABLE previews;

PRAGMA foreign_key_check;
PRAGMA integrity_check;
```

`system_config` stores only values that differ from their defaults, so the
legacy configuration row exists only when an operator changed
`PREVIEW_LOG_REF_LIST_LIMIT`. The `DELETE` is safe when no such row exists and
must run before starting the new revision: persisted keys absent from the typed
configuration registry prevent startup. `DROP TABLE` also removes the legacy
preview indexes. The application tolerates an extra table, so omitting that
statement does not block schema admission, but the upgrade is incomplete and
generated-data cleanup no longer manages that operator-visible legacy object.
Old `p-*` payloads are disposable cache and are removed by normal startup cache
cleanup; they are not migrated to `statement_previews`.

`PRAGMA foreign_key_check` must return no rows and `PRAGMA integrity_check` must
return `ok`. Start the application only after both checks pass, then confirm
application schema admission before reopening traffic.

Keep the application data root and database private to the runtime user. The installer applies mode `0700` to `/var/lib/polygon-replica`, and systemd uses `UMask=0077`.

Systemd:

```bash
cd /opt/polygon-replica
runtime_user="$(sudo systemctl show polygon-replica.service --property=User --value)"
test -n "$runtime_user"
test "$runtime_user" != root
sudo systemctl stop polygon-replica.service
sudo -u "$runtime_user" git pull --ff-only
sudo -u "$runtime_user" python3.14 -m venv --clear .venv
sudo -u "$runtime_user" .venv/bin/python -m pip install --upgrade pip
sudo -u "$runtime_user" .venv/bin/python -m pip install -r requirements.txt
sudo systemctl start polygon-replica.service
sudo journalctl -u polygon-replica.service -n 200 --no-pager
```

Compose:

```bash
git pull --ff-only
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs --tail=200 app
```

Do not run old and new application revisions against the same writable roots.

## Restart

Enable **Pause admission** and wait for active work to drain before using
**Restart application**. The action exits the process and requires systemd,
Compose, or another supervisor to start it again. If work cannot drain, **Force
restart** interrupts process-local work; startup reconciliation marks the
affected durable jobs failed. **Resume admission** cancels the maintenance
preparation.

## Backup

Enable **Pause admission**, wait for active work to drain, then use **Create
source backup** and **Download latest backup**. The archive is one point-in-time
recovery set containing SQLite and the complete bare Git, workspace, and contest
source roots. Derived data, caches, application code, the encryption key, and
deployment configuration are excluded. The exact archive layout is defined by
the [storage protocol](../protocol/storage.md#source-backup).

## Restore

1. Stop the application and judgehosts.
2. Extract the archive into an isolated staging directory.
3. Restore `database/metadata.db`, `bare/`, `workspaces/`, and
   `contest-sources/` as one set, without archive `-wal` or `-shm` files, then
   restore runtime ownership and modes.
4. Start the application revision and deployment configuration retained
   alongside the backup, then validate them before reopening traffic.

Restore the database and source roots only as one recovery set.
