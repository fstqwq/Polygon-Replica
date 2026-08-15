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

The installer verifies CPython 3.14, installs host packages, configures user
namespaces, creates runtime roots, probes bubblewrap and TeX as `polygon`, builds
`.venv` as that account, writes `/etc/polygon-replica.env`, renders and verifies
the systemd unit, and starts it. Direct root runtime is rejected. The environment
file is owned by root with mode `0600`; rerunning the installer refreshes its
managed paths while preserving valid operator-managed assignments and `#` or
`;` comments. Shell-style input assignments with an optional `export` prefix
are accepted during migration and rewritten as systemd `NAME=VALUE` records;
unbalanced quotes and multiline continuations are rejected before replacement.

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

Losing or changing the key makes the stored SMTP password unreadable. Installer
reruns preserve this operator-owned assignment. Duplicate keys or records that
are not single-line environment assignments make the installer stop before
replacing the active file.

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

Create the permanent backup bind and start Compose. The following creates a new
private `.env`; edit an existing file instead of overwriting it:

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

## TLS proxy

Install nginx and certbot:

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

Create `/etc/nginx/sites-available/polygon-replica`:

```nginx
server {
    server_name <domain>;
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

Enable it and issue the certificate:

```bash
sudo ln -sf /etc/nginx/sites-available/polygon-replica \
  /etc/nginx/sites-enabled/polygon-replica
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d <domain>
```

Do not expose port 8001 publicly. Review trusted proxy headers if the proxy is
not local.

## First use and Judgehost

Open `https://<domain>/`, complete initial setup, and create the administrator.
In Settings configure and save:

```text
JUDGEHOST_ENABLE = true
JUDGEHOST_API_USERNAME = judgehost
JUDGEHOST_API_TOKEN = <strong random secret>
```

Use the Settings-generated Judgehost command. Each daemon needs a unique
hostname, daemon id, CPU assignment, and unused `RUN_USER_UID_GID`. Verify ids
with `getent passwd <id>` and `getent group <id>`. A same-host Docker Judgehost
normally reaches `http://host.docker.internal:8001/` through the configured host
gateway. The generated command also maps each container hostname to `127.0.1.1`
so local tools such as `sudo` can resolve it. The generated image is currently
`domjudge/judgehost:latest`; operators own image pinning and upgrade validation.

## Upgrade

Create and download a source backup from Admin first. Then update one deployment
at a time.

Application startup does not alter an existing SQLite schema. Before installing
a revision that changes required tables, columns, or named indexes, stop the
service and apply that revision's explicit offline database procedure. If this
step is missed, the process remains available only as a raw `503` diagnostic
that lists the missing schema objects; no workers or Judgehost runtime start.
Extra tables, columns, indexes, and rows do not block startup and are preserved.

Systemd:

```bash
cd /opt/polygon-replica
sudo systemctl stop polygon-replica.service
sudo -u polygon git pull --ff-only
sudo -u polygon python3.14 -m venv --clear .venv
sudo -u polygon .venv/bin/python -m pip install --upgrade pip
sudo -u polygon .venv/bin/python -m pip install -r requirements.txt
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

## Backup

Open Admin and enable **Pause admission**. New business requests and top-level
jobs receive a temporary maintenance response while already admitted worker and
Judgehost work finishes. Admin reads remain available, and idle Judgehosts get
an immediate empty fetch response instead of long-polling. When the displayed
active counts reach zero, use **Create source backup**. The operation closes the
remaining runtime boundary and archives the complete bare Git and workspace
roots. `/maintenance` shows progress.

After it succeeds, use **Download latest backup**. The application retains one
published file at `backup_root/source-backup/latest.tar.gz`; a later successful
run atomically replaces it. Move the downloaded file to independent off-host
storage if it must survive loss of the application host.

The archive contains committed problem history and every workspace, including
uncommitted files. It deliberately excludes SQLite, Contest source and
attachments, derived data, caches, other backup-root content, application code,
the encryption key, and TLS/proxy configuration. Keep secrets and deployment
configuration under the operator's separate secret/configuration backup policy.
This source archive is not a full application-state backup.

The same drained state enables **Restart application**. That action exits the
single process after sending the HTTP response; the installed systemd unit or
the checked-in Compose restart policy starts a new process. Do not use it when
running uvicorn without a supervisor. Use **Resume admission** to cancel a
maintenance preparation without starting an operation.

## Restore

Restore is an operator-managed source recovery procedure:

1. Stop the application and Judgehosts.
2. Inspect `manifest.json` and the member paths before extraction.
3. Extract into an isolated staging directory, not over live roots.
4. Replace the configured bare Git and workspace roots from the staged `bare/`
   and `workspaces/` trees, then restore runtime ownership.
5. Reconcile or recreate the SQLite metadata that identifies those repositories
   and workspaces; it is not present in this archive.
6. Start one application process and validate repository history and workspace
   contents before reopening traffic.

If an operator also retains a matching SQLite copy, it may be restored through
the operator's offline procedure. Mixing source roots with unrelated SQLite
metadata is not a supported point-in-time restore.
