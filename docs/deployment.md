# Deployment

This document describes the production-style deployment flow for Polygon-Replica.

Scope:

- install the web service on a Debian/Ubuntu host
- run the web app under `systemd`
- terminate public TLS at `nginx`
- connect DOMjudge judgehost containers or machines to the web service

This document intentionally uses production Linux paths. Do not add workstation-specific
or development-environment absolute paths here.

## Host Assumptions

Assume:

- Debian or Ubuntu
- the target DNS name points to the host
- ports `80` and `443` are reachable
- the operator has `sudo`
- the `judgehost` Unix user and group exist
- the web app listens on `127.0.0.1:8001`; nginx is the public TLS entrypoint

The bundled systemd unit expects the code checkout at:

```text
/opt/polygon-replica
```

The bundled installer prepares runtime state under:

```text
/srv/polygon-replica
/var/lib/polygon-replica
/tmp/polygon-replica
```

## Fetch The Code

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone <repo-url> polygon-replica
sudo chown -R judgehost:judgehost polygon-replica
cd polygon-replica
```

## Run The Host Installer

Run the installer as the same Unix user that will run the service. With the bundled
systemd unit, that user is `judgehost`. The installer uses `sudo` for system changes,
so the runtime user must be allowed to use `sudo` during installation.

```bash
sudo -H -u judgehost bash -lc 'cd /opt/polygon-replica && ./scripts/install_host.sh'
```

The installer:

- installs system packages
- enables user namespaces for sandboxing
- prepares runtime storage roots
- probes `bubblewrap`
- probes TeX support
- creates `.venv`
- installs `requirements.txt`
- writes `/etc/polygon-replica.env`
- installs and starts `polygon-replica.service`

## Runtime Environment

The installer writes `/etc/polygon-replica.env`.

Review it before exposing the service publicly. A production deployment should set:

```bash
export POLYGON_REPLICA_AUTH_COOKIE_SECURE=1
```

Runtime roots should stay outside the repository checkout. The default installer values are:

```bash
export POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db
export POLYGON_REPLICA_BARE_ROOT=/srv/polygon-replica/git
export POLYGON_REPLICA_WORKSPACE_ROOT=/srv/polygon-replica/workspaces
export POLYGON_REPLICA_ARTIFACTS_ROOT=/srv/polygon-replica/export
export POLYGON_REPLICA_CACHE_ROOT=/tmp/polygon-replica
```

SMTP password storage uses reversible authenticated encryption. If administrators will configure
SMTP in Settings, set a stable 32-byte base64url key:

```bash
export POLYGON_REPLICA_ENCRYPTION_KEY="$(python3 -c 'import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))')"
```

Keep this value stable across restarts. Changing it requires re-entering the SMTP password.

## Service Management

```bash
sudo systemctl status polygon-replica.service
sudo systemctl restart polygon-replica.service
sudo journalctl -u polygon-replica.service -n 200 --no-pager
```

The installed service file is `scripts/systemd/polygon-replica.service`.

## Nginx

Install nginx and certbot:

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

Create an nginx site for the target domain:

```bash
sudo tee /etc/nginx/sites-available/polygon-replica >/dev/null <<'EOF'
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
EOF
```

Enable the site:

```bash
sudo ln -sf /etc/nginx/sites-available/polygon-replica /etc/nginx/sites-enabled/polygon-replica
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

Issue the certificate:

```bash
sudo certbot --nginx -d <domain>
```

## First Web Setup

Open:

```text
https://<domain>/
```

Complete setup, create the admin user, then open Settings and configure:

```text
JUDGEHOST_ENABLE = true
JUDGEHOST_API_USERNAME = judgehost
JUDGEHOST_API_TOKEN = <strong secret>
```

Save these values before starting judgedaemon workers.

## Judgehosts

The web UI can generate judgedaemon container commands from Settings.

Judgehosts must reach the web service at the configured base URL. A common Docker-on-same-host
setup uses:

```text
http://host.docker.internal:8001/
```

For multiple judgehosts, assign separate container names, hostnames, and CPU sets. Keep the
`JUDGEDAEMON_USERNAME` and `JUDGEDAEMON_PASSWORD` values aligned with Settings.

For consistent judgedaemon performance, consider isolating judgehost CPU cores with
`isolcpus` and disabling turbo boost on hosts dedicated to judging.

## Smoke Checks

Check the service stack:

```bash
sudo systemctl status polygon-replica.service
sudo systemctl status nginx
sudo docker ps
```

Check the public entrypoint:

```bash
curl -I https://<domain>/
```

Expected result after setup:

```text
HTTP redirect to /login or /problems
```

Check runtime paths:

```bash
ls -ld /srv/polygon-replica /var/lib/polygon-replica /tmp/polygon-replica
```

## Upgrade

```bash
cd /opt/polygon-replica
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart polygon-replica.service
```

Do not store production runtime data inside the repository checkout.
