# Operator deployment runbook

This runbook covers the two supported single-process layouts: a Debian/Ubuntu
host with systemd, or the checked-in Docker Compose service. Substitute values
shown as `<...>`. Keep runtime data outside the checkout.

## Before installation

Prepare a DNS name, ports 80/443, and one non-root service account. The examples
use `polygon` and `/opt/polygon-replica`:

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

The installer installs host packages, configures user namespaces, creates
runtime roots, probes bubblewrap and TeX as `polygon`, builds `.venv` as that
account, writes `/etc/polygon-replica.env`, renders and verifies the systemd
unit, and starts it. Direct root runtime is rejected.

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

Before adding the key, restrict the installer-created environment file, then add
`POLYGON_REPLICA_ENCRYPTION_KEY=<key>` and restart:

```bash
sudo chmod 0600 /etc/polygon-replica.env
sudoedit /etc/polygon-replica.env
sudo systemctl restart polygon-replica.service
```

Losing or changing the key makes the stored SMTP password unreadable. Re-running
the installer replaces this file, so preserve and restore the key afterward.

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
gateway. The generated image is currently `domjudge/judgehost:latest`; operators
own image pinning and upgrade validation.

## Upgrade

Create a backup first. Then update one deployment at a time.

Systemd:

```bash
cd /opt/polygon-replica
sudo systemctl stop polygon-replica.service
sudo -u polygon git pull --ff-only
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
Project-owned removed shapes are not kept through compatibility layers.

## Backup

Backups must be taken while the application is stopped so SQLite, Git, and
filesystem roots describe one point in time. Also retain the encryption key and
TLS/proxy configuration through the operator's secret/configuration backup.
`cache_root` is disposable and is not backed up.

For systemd, stop the service and archive the durable roots plus complete
artifacts if desired:

```bash
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
sudo systemctl stop polygon-replica.service
sudo tar --acls --xattrs -C / -czf \
  "/var/backups/polygon-replica/system-$stamp.tgz" \
  var/lib/polygon-replica \
  srv/polygon-replica
sudo systemctl start polygon-replica.service
```

For Compose, stop the app, discover the actual volume names with
`docker volume ls`, and mount the `srv` and `var` volumes read-only into a backup
container:

```bash
sudo docker compose stop app
sudo docker run --rm \
  -v <project>_srv:/srv/polygon-replica:ro \
  -v <project>_var:/var/lib/polygon-replica:ro \
  -v /var/backups/polygon-replica:/backup \
  alpine sh -c 'tar czf /backup/compose.tgz /srv/polygon-replica /var/lib/polygon-replica'
sudo docker compose start app
```

The separately mounted backup root is permanent operator data; include it in an
off-host backup policy rather than recursively archiving it into itself.

## Restore

Restore onto an empty replacement location or move the current roots aside; do
not overlay an archive onto a running or partially populated installation.

1. Stop the application and Judgehosts.
2. Verify the archive checksum and inspect its member paths.
3. Restore `/var/lib/polygon-replica` and `/srv/polygon-replica` (or the matching
   Compose volumes), then restore the same encryption key and proxy/TLS config.
4. Set ownership to the configured runtime uid/gid.
5. Start only one application process and inspect startup reconciliation logs.
6. Check login, published problems, workspace status, artifact downloads,
   contest attachments, and Judgehost registration before reopening traffic.

After an in-place recovery, keep the displaced roots until the restored service
has passed these checks. Never restore `cache_root`; startup recreates it.
