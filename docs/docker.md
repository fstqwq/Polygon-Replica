# Docker Deployment

This document describes running Polygon-Replica as a Docker container. It is a
parallel deployment path to [deployment.md](deployment.md); the two layouts use
the same runtime user (`judgehost`) and the same runtime roots
(`/srv/polygon-replica`, `/var/lib/polygon-replica`, `/tmp/polygon-replica`),
just relocated into named volumes inside Docker.

TLS termination is left on the host nginx + certbot for parity with the
non-Docker deployment.

## Host Assumptions

- Ubuntu 24.04 (or any distro) with Docker Engine + Compose plugin installed.
- Ports 80 and 443 reachable for nginx; 8001 stays on loopback.
- The host operator has `sudo`.

## Host Sysctl Prep

Bubblewrap inside the container reuses the host kernel's user-namespace
support. Apply the same sysctl set the host installer would have written:

```bash
sudo tee /etc/sysctl.d/99-polygon-replica-sandbox.conf >/dev/null <<'EOF'
kernel.unprivileged_userns_clone = 1
user.max_user_namespaces = 1048576
kernel.apparmor_restrict_unprivileged_userns = 0
EOF
sudo sysctl --system
```

The third line only applies on hosts that expose
`/proc/sys/kernel/apparmor_restrict_unprivileged_userns`. Ubuntu 24.04 does;
older kernels can ignore the warning.

## Optional: Stable SMTP Encryption Key

If administrators will configure SMTP in Settings, set a stable 32-byte
base64url key once and keep it stable across restarts (changing it requires
re-entering the SMTP password). Add it to a local `.env` next to
`docker-compose.yml`:

```bash
python3 -c 'import base64, secrets; print("POLYGON_REPLICA_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))' >> .env
```

Compose will load `.env` automatically, but you also need to forward the value
into the container by adding it to the `environment:` block:

```yaml
    environment:
      POLYGON_REPLICA_ENCRYPTION_KEY: ${POLYGON_REPLICA_ENCRYPTION_KEY:-}
```

## Build And Run

From the repository root:

```bash
sudo docker compose build
sudo docker compose up -d
sudo docker compose logs -f app
```

The container exposes the web app on `127.0.0.1:8001`. Volumes
`srv`, `var`, and `cache` hold the same content as the host paths under
`/srv/polygon-replica`, `/var/lib/polygon-replica`, and `/tmp/polygon-replica`.

The entrypoint runs a bubblewrap probe and exits non-zero if user namespaces
are not available. If you see that error, recheck the sysctl prep above.

## Nginx (On The Host)

Reuse the nginx site from [deployment.md](deployment.md) verbatim — the app
still listens on `127.0.0.1:8001`, just inside a container. Issue the
certificate the same way (`certbot --nginx -d <domain>`).

## First Web Setup

Open `https://<domain>/`, complete setup, create the admin user, then in
Settings configure:

```text
JUDGEHOST_ENABLE       = true
JUDGEHOST_API_USERNAME = judgehost
JUDGEHOST_API_TOKEN    = <strong secret>
```

## Judgehosts

Judgehost containers run as before:

```bash
sudo docker run -d --name judgehost-0 \
  --add-host=host.docker.internal:host-gateway \
  --hostname judgehost-0 \
  --privileged \
  -e DAEMON_ID=0 \
  -e JUDGEDAEMON_USERNAME=judgehost \
  -e JUDGEDAEMON_PASSWORD=<token from Settings> \
  -e DOMSERVER_BASEURL=http://host.docker.internal:8001/ \
  domjudge/judgehost:latest
```

The web UI's Settings page generates the exact command for your tokens.

## Upgrade

```bash
cd /opt/polygon-replica   # or wherever the checkout lives
git pull
sudo docker compose build
sudo docker compose up -d
```

Volumes survive the rebuild; SQLite metadata, git bares, workspaces, and
exports are preserved.

## Backups

Back up the `srv` and `var` volumes. Compose prefixes volume names with the
project name (the working-directory name, lowercased); confirm with:

```bash
sudo docker volume ls
```

Snapshot example (substitute the actual volume names from `docker volume ls`):

```bash
sudo docker run --rm \
  -v polygon-replica_srv:/srv/polygon-replica:ro \
  -v polygon-replica_var:/var/lib/polygon-replica:ro \
  -v "$(pwd)":/backup \
  alpine \
  tar czf /backup/polygon-replica-$(date +%Y%m%d).tgz \
  /srv/polygon-replica /var/lib/polygon-replica
```

The `cache` volume is regeneration-safe and does not need backups.

## Troubleshooting

- **`bubblewrap probe failed inside the container.`** — host sysctl is missing.
  Re-run the prep section, then `sudo docker compose restart app`.
- **App reachable from the host but not from a judgehost container** — confirm
  the judgehost runs with `--add-host=host.docker.internal:host-gateway`, or
  attach both containers to the same Docker network.
- **TeX errors during PDF generation** — the image bakes pdflatex/xelatex
  formats at build time. If formats look stale, force a rebuild with
  `sudo docker compose build --no-cache app`.
