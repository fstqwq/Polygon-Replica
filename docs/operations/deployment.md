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

Open `https://<domain>/` and complete initial setup. Setup creates the administrator with a trusted email address and saves the email allow regex used by later public registrations. It also displays the effective storage paths for operator confirmation.
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
so local tools such as `sudo` can resolve it.

### Judgehost image choice

The generated command ends with `domjudge/judgehost:latest`. This is the
simplest installation path: Docker downloads a public, maintained image and no
Judgehost toolchain needs to be built locally. `latest` is a moving tag,
however, so record the image digest used by a production deployment and roll a
new digest through one Judgehost before replacing the rest of a fleet.

For long-lived Judgehosts, the currently recommended source-built alternative
is [`fstqwq/domjudge`](https://github.com/fstqwq/domjudge) at commit
`6eb5e99d352c4f1ef70f540378b6cf069abef6be`. It contains the corresponding
upstream 10.0 development line plus these two operational fixes:

- [`b52a97af`](https://github.com/fstqwq/domjudge/commit/b52a97af01f96cbe39267a917503393c548e9701)
  resets a testcase result directory before a retry. A task fetched again by
  the same daemon therefore cannot consume output, feedback, or hardlinks left
  by an interrupted attempt.
- [`6eb5e99d`](https://github.com/fstqwq/domjudge/commit/6eb5e99d352c4f1ef70f540378b6cf069abef6be)
  reclaims the downloaded testcase cache when ordinary judging-directory
  cleanup cannot restore the configured free-space threshold. The cache is
  retained during normal operation; under disk pressure, cache misses and
  re-downloads are preferred to disabling the Judgehost.

That source also includes performance-oriented upstream changes from the 10.0
development line, including removing shell and subprocess work from frequently
used judgedaemon paths. This improves throughput for workloads with many short
testcases. The operator must build and distribute the image, test updates,
rebuild it for security fixes, and deliberately advance both source revisions.
The development-line source changes more frequently than a public release.

Build the pinned source with DOMjudge's separate packaging repository on a
Linux Docker host. The packaging build starts a temporary privileged container
to construct the Judgehost chroot:

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

Keep the credentials, mounts, resource limits, cgroup settings, hostname, and
daemon identity from the command generated by Settings; replace only its final
image reference with `polygon-judgehost:10.0-dev-6eb5e99`. A tag built on one
machine is local to that Docker daemon. Push it to a private registry or transfer
it with `docker save` and `docker load` before using it on another host, and pin
the distributed digest in production. Start one daemon first and confirm task
fetch, compilation, judging, retry behavior, disk cleanup, and callback delivery
before scaling the image to the full fleet.

## Upgrade

Create and download a source backup from Admin first. Then update one deployment
at a time.

Application startup does not alter an existing SQLite schema. Before installing
a revision that changes required tables, columns, or named indexes, compare the
canonical schema in `app/db.py` at the deployed commit with the target commit.
Stop the Web process, workers, and Judgehosts, back up SQLite together with its
WAL/SHM files, and apply a one-off offline migration for that exact diff. Keep
IDs and relationships intact, then require `foreign_key_check`,
`integrity_check`, and application schema admission to pass before reopening
traffic. The migration is an operator procedure and does not belong in Git.

The latest breaking database change is
[`b16617c` (`Simplify Contest authoring workflows`)](https://github.com/fstqwq/Polygon-Replica/commit/b16617c98579c60a2ad8e6e449d131539bc0ed18).
Deployments whose current commit predates it must upgrade the database for the
complete schema diff between their deployed revision and the target revision.

The bearer credential is replayable by design. Keep the application data root
and database private to the runtime user. The host installer applies mode
`0700` to `/var/lib/polygon-replica`, and the systemd unit uses `UMask=0077` so
new database, WAL, and SHM files are not readable by other local users.

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
remaining runtime boundary and archives a transactionally consistent SQLite
snapshot together with the complete bare Git, workspace, and Contest source
roots. `/maintenance` shows progress.

After it succeeds, use **Download latest backup**. The application retains one
published file at `backup_root/source-backup/latest.tar.gz`; a later successful
run atomically replaces it. Move the downloaded file to independent off-host
storage if it must survive loss of the application host.

The archive contains `database/metadata.db`, committed problem history, every
workspace including uncommitted files, and Contest statement sources and
attachments. SQLite is copied with its online backup API, so transactions
committed in WAL are included without copying `-wal` or `-shm` files. Derived
data, caches, other backup-root content, application code, the encryption key,
and TLS/proxy configuration are excluded. Keep secrets and deployment
configuration under the operator's separate secret/configuration backup policy.
The archive itself is sensitive because SQLite contains authentication,
session, access-control, and encrypted configuration records; store it encrypted
and off host.

The same drained state enables **Restart application**. That action exits the
single process after sending the HTTP response; the installed systemd unit or
the checked-in Compose restart policy starts a new process. Do not use it when
running uvicorn without a supervisor. If active work cannot drain because the
runtime is stuck, **Force restart** is available beside the disabled normal
restart action. It requires admission to be paused but deliberately ignores the
active-work counts; unfinished process-local work is interrupted and startup
reconciliation marks its durable jobs failed. Use **Resume admission** to
cancel a maintenance preparation without starting an operation.

## Restore

Restore is an operator-managed recovery procedure:

1. Stop the application and Judgehosts.
2. Inspect `manifest.json` and the member paths before extraction.
3. Extract into an isolated staging directory, not over live roots.
4. Replace the configured bare Git, workspace, and Contest source roots from the
   staged `bare/`, `workspaces/`, and `contest-sources/` trees, then restore
   runtime ownership.
5. Replace the configured SQLite database with `database/metadata.db`; do not
   restore archive `-wal` or `-shm` files. Restore database ownership and mode.
6. Start one application process and validate schema startup, repository
   history, workspaces, Contest statement sources, and authentication before
   reopening traffic.

The archive is manual disaster-recovery material rather than a versioned import
format. Retain the application revision and deployment configuration used to
create it, inspect its contents before restore, and adapt the offline restore
procedure when application storage changes. Do not mix source roots with an
unrelated database.
