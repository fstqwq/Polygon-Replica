# Deployment

This document describes a production-style deployment for `polygon.fstqwq.pw`-like hosts.

Scope:

- install the current project on a fresh Debian/Ubuntu host
- run the web app under `systemd`
- terminate TLS at `nginx` on `443`
- run three Docker judgehosts
- isolate CPUs correctly for judgedaemon containers

This is not the local development flow. For local startup, use [INSTALL.md](/C:/code/Polygon-Replica/INSTALL.md) and `./scripts/start_local.sh`.

## 1. Host Assumptions

Assume:

- Debian or Ubuntu
- public DNS already points the domain to this machine
- you have `sudo`
- ports `80` and `443` are reachable from the internet
- you will run the web app on `127.0.0.1:8001`

Recommended runtime paths:

- code: `/opt/polygon-replica`
- DB: `/var/lib/polygon-replica/metadata.db`
- bare repos: `/srv/git`
- workspaces: `/srv/workspaces`
- runs: `/srv/runs`
- artifacts: `/var/lib/polygon-replica/artifacts`
- cache: `/var/cache/polygon-replica`

## 2. Fetch The Code

Preferred:

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone <repo-url> polygon-replica
sudo chown -R "$USER":"$USER" /opt/polygon-replica
cd /opt/polygon-replica
```

If GitHub deploy keys are not ready yet, copy a repository snapshot to `/opt/polygon-replica` instead. The rest of this document is unchanged.

## 3. Run The Host Installer

From the repository root:

```bash
cd /opt/polygon-replica
./scripts/install_host.sh
```

The installer does all of the following:

- installs apt dependencies
- enables unprivileged user namespaces
- prepares `/srv/*`, `/var/lib/polygon-replica`, `/var/cache/polygon-replica`
- probes `bubblewrap`
- probes TeX
- creates `.venv`
- installs `requirements.txt`
- writes `/etc/polygon-replica.env`

At this point you should have:

- `/opt/polygon-replica/.venv`
- `/etc/polygon-replica.env`
- empty runtime roots under `/srv` and `/var/lib/polygon-replica`

## 4. Production Environment File

The installer writes a good default `/etc/polygon-replica.env`. For production, review it and add the secure-cookie setting:

```bash
sudoedit /etc/polygon-replica.env
```

Recommended content:

```bash
export POLYGON_REPLICA_DB=/var/lib/polygon-replica/metadata.db
export POLYGON_REPLICA_BARE_ROOT=/srv/git
export POLYGON_REPLICA_WORKSPACE_ROOT=/srv/workspaces
export POLYGON_REPLICA_RUN_ROOT=/srv/runs
export POLYGON_REPLICA_ARTIFACTS_ROOT=/var/lib/polygon-replica/artifacts
export POLYGON_REPLICA_CACHE_ROOT=/var/cache/polygon-replica
export POLYGON_REPLICA_AUTH_COOKIE_SECURE=1
```

Do not point production at `./var` or other repository-relative paths. Production should use absolute system paths.

## 5. Systemd Service

Create the service:

```bash
sudo tee /etc/systemd/system/polygon-replica.service >/dev/null <<'EOF'
[Unit]
Description=Polygon Replica
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polygon-replica
EnvironmentFile=/etc/polygon-replica.env
ExecStart=/opt/polygon-replica/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 --proxy-headers --forwarded-allow-ips=*
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
```

Then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polygon-replica.service
sudo systemctl status polygon-replica.service
```

At this point, plain HTTP on `127.0.0.1:8001` should work locally.

## 6. Install Docker

If Docker is not already installed:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
docker --version
```

## 7. Configure CPU Isolation

For three judgehosts, isolate three CPU pairs. Example:

- judgehost 1: `1,5`
- judgehost 2: `2,6`
- judgehost 3: `3,7`

Edit GRUB:

```bash
sudoedit /etc/default/grub
```

Add these kernel parameters to `GRUB_CMDLINE_LINUX_DEFAULT`:

```text
isolcpus=1-3,5-7 nohz_full=1-3,5-7 rcu_nocbs=1-3,5-7
```

Apply and reboot:

```bash
sudo update-grub
sudo reboot
```

After reboot, verify:

```bash
cat /proc/cmdline
```

You should see:

```text
isolcpus=1-3,5-7 nohz_full=1-3,5-7 rcu_nocbs=1-3,5-7
```

## 8. Configure Nginx On 443

Install nginx and certbot:

```bash
sudo apt-get update
sudo apt-get install -y nginx certbot python3-certbot-nginx
```

Create the site:

```bash
sudo tee /etc/nginx/sites-available/polygon-replica >/dev/null <<'EOF'
server {
    server_name polygon.fstqwq.pw;
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

Enable it:

```bash
sudo ln -sf /etc/nginx/sites-available/polygon-replica /etc/nginx/sites-enabled/polygon-replica
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx
```

Issue the certificate:

```bash
sudo certbot --nginx -d polygon.fstqwq.pw
```

Verify renewal:

```bash
sudo systemctl status certbot.timer
sudo certbot renew --dry-run
```

After this, the public entrypoint should be:

- `https://polygon.fstqwq.pw/`

## 9. First Web Initialization

Open:

- `https://polygon.fstqwq.pw/`

Complete setup and create the admin user. Example:

- username: `admin`
- password: `admin123!`

Then go to:

- `https://polygon.fstqwq.pw/problems/admin/settings`

Set judgehost runtime values:

- `JUDGEHOST_ENABLE = true`
- `JUDGEHOST_API_USERNAME = judgehost`
- `JUDGEHOST_API_TOKEN = <strong secret>`

Save them before starting containers.

## 10. Start Three Judgehost Containers

The web UI can generate these commands for you from the Settings page. The current project expects DOMjudge judgehosts talking to:

- `http://host.docker.internal:8001/`

Example deployment with three judgehosts and explicit CPU pinning:

```bash
sudo docker run -d --restart unless-stopped \
  --privileged --cgroupns=host --storage-opt size=10G \
  --cpuset-cpus=1,5 \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --add-host=host.docker.internal:host-gateway \
  --name judgehost-1 \
  --hostname judgedaemon-1 \
  -e DAEMON_ID=1 \
  -e CONTAINER_TIMEZONE=Asia/Shanghai \
  -e DOMSERVER_BASEURL=http://host.docker.internal:8001/ \
  -e JUDGEDAEMON_USERNAME=judgehost \
  -e JUDGEDAEMON_PASSWORD='<same token as JUDGEHOST_API_TOKEN>' \
  domjudge/judgehost:latest

sudo docker run -d --restart unless-stopped \
  --privileged --cgroupns=host --storage-opt size=10G \
  --cpuset-cpus=2,6 \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --add-host=host.docker.internal:host-gateway \
  --name judgehost-2 \
  --hostname judgedaemon-2 \
  -e DAEMON_ID=2 \
  -e CONTAINER_TIMEZONE=Asia/Shanghai \
  -e DOMSERVER_BASEURL=http://host.docker.internal:8001/ \
  -e JUDGEDAEMON_USERNAME=judgehost \
  -e JUDGEDAEMON_PASSWORD='<same token as JUDGEHOST_API_TOKEN>' \
  domjudge/judgehost:latest

sudo docker run -d --restart unless-stopped \
  --privileged --cgroupns=host --storage-opt size=10G \
  --cpuset-cpus=3,7 \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --add-host=host.docker.internal:host-gateway \
  --name judgehost-3 \
  --hostname judgedaemon-3 \
  -e DAEMON_ID=3 \
  -e CONTAINER_TIMEZONE=Asia/Shanghai \
  -e DOMSERVER_BASEURL=http://host.docker.internal:8001/ \
  -e JUDGEDAEMON_USERNAME=judgehost \
  -e JUDGEDAEMON_PASSWORD='<same token as JUDGEHOST_API_TOKEN>' \
  domjudge/judgehost:latest
```

Check them:

```bash
sudo docker ps
```

Then confirm in the web UI:

- `Settings`
- judgehost should show `online 3/3`

## 11. Smoke Checks

Check the service stack:

```bash
sudo systemctl status polygon-replica.service
sudo systemctl status nginx
sudo docker ps
```

Check the public entrypoint:

```bash
curl -I https://polygon.fstqwq.pw/
```

Expected result after setup:

- HTTP `303` to `/login` or `/problems`

Check runtime paths:

```bash
ls -ld /srv/git /srv/workspaces /srv/runs
ls -ld /var/lib/polygon-replica /var/cache/polygon-replica
```

## 12. Upgrades

For code-only upgrades:

```bash
cd /opt/polygon-replica
git pull
/opt/polygon-replica/.venv/bin/pip install -r requirements.txt
sudo systemctl restart polygon-replica.service
```

If you deploy by snapshot instead of `git pull`, replace the code under `/opt/polygon-replica`, then restart the service.

Do not keep old production data around when the code intentionally breaks old shapes. This project explicitly prefers reset-over-migration for incompatible runtime data.

## 13. Notes

- `./scripts/install_host.sh` is the host bootstrapper. Use it once per host, not as the ongoing service runner.
- `./scripts/start_local.sh` is for local development, not production.
- The authoritative public service should be `nginx -> 127.0.0.1:8001`.
- judgehost credentials are configured in the app, and the same values must be passed into each judgedaemon container.
