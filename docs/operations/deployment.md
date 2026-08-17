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

### Contest problem-index upgrade

The release that removes independent Contest roster positions requires this
one-time offline upgrade for a database whose `contest_problems` still contains
`position` and `label`. A database that already contains
`contest_problems.idx` and `contest_build_items.ordinal` must not run it again.

Stop the Web process, workers, and Judgehosts. Set `DB_PATH` to the configured
SQLite file, then retain the database and any existing WAL/SHM files together:

```bash
backup_dir="$(dirname "$DB_PATH")/contest-idx-backup-$(date +%Y%m%d-%H%M%S)"
install -d -m 0700 "$backup_dir"
for suffix in '' '-wal' '-shm'; do
  if test -e "$DB_PATH$suffix"; then
    cp --preserve=all "$DB_PATH$suffix" "$backup_dir/"
  fi
done
sqlite3 "$DB_PATH" 'PRAGMA wal_checkpoint(TRUNCATE);'
```

Before changing the schema, this query must return no rows. Any result means
the old labels cannot become canonical unique indices without an operator
decision:

```bash
sqlite3 "$DB_PATH" <<'SQL'
SELECT contest_id, idx, COUNT(*) AS copies
FROM (
    SELECT contest_id, UPPER(TRIM(label)) AS idx
    FROM contest_problems
)
GROUP BY contest_id, idx
HAVING copies > 1
    OR idx = ''
    OR length(idx) > 16
    OR substr(idx,1,1) NOT GLOB '[A-Z0-9]'
    OR idx GLOB '*[^A-Z0-9._-]*';
SQL
```

Run the following directly from the shell; do not save it in the checkout. The
two `before` and `after` counts must match. Historical build positions are
copied unchanged to `ordinal`, while current roster rows immediately acquire
the application's natural `idx` ordering when read:

```bash
sqlite3 -bail "$DB_PATH" <<'SQL'
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

SELECT 'contest_problems before', COUNT(*) FROM contest_problems;
SELECT 'contest_build_items before', COUNT(*) FROM contest_build_items;

DROP INDEX IF EXISTS idx_contest_problems_problem;
DROP INDEX IF EXISTS idx_contest_build_items_job_position;
DROP INDEX IF EXISTS idx_contest_build_items_materialization;

ALTER TABLE contest_problems RENAME TO contest_problems_legacy;
CREATE TABLE contest_problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL,
    idx TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    statement_folder TEXT NOT NULL DEFAULT '',
    added_by_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contest_id, problem_id),
    UNIQUE(contest_id, idx),
    FOREIGN KEY(contest_id) REFERENCES contests(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(added_by_user_id) REFERENCES users(id)
);
INSERT INTO contest_problems(
    id,contest_id,idx,problem_id,statement_folder,added_by_user_id,created_at
)
SELECT id,contest_id,UPPER(TRIM(label)),problem_id,statement_folder,
       added_by_user_id,created_at
FROM contest_problems_legacy;

ALTER TABLE contest_build_items RENAME TO contest_build_items_legacy;
CREATE TABLE contest_build_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    contest_problem_id INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    idx TEXT NOT NULL,
    problem_id INTEGER NOT NULL,
    statement_folder TEXT NOT NULL DEFAULT '',
    source_commit TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    materialization_id TEXT,
    archive_sha256 TEXT,
    UNIQUE(job_id,contest_problem_id),
    FOREIGN KEY(job_id) REFERENCES contest_jobs(id),
    FOREIGN KEY(problem_id) REFERENCES problems(id),
    FOREIGN KEY(materialization_id) REFERENCES problem_package_materializations(id)
);
INSERT INTO contest_build_items(
    id,job_id,contest_problem_id,ordinal,idx,problem_id,statement_folder,
    source_commit,revision_number,materialization_id,archive_sha256
)
SELECT id,job_id,contest_problem_id,position,UPPER(TRIM(label)),problem_id,
       statement_folder,source_commit,revision_number,materialization_id,
       archive_sha256
FROM contest_build_items_legacy;

SELECT 'contest_problems after', COUNT(*) FROM contest_problems;
SELECT 'contest_build_items after', COUNT(*) FROM contest_build_items;

DROP TABLE contest_build_items_legacy;
DROP TABLE contest_problems_legacy;
CREATE INDEX idx_contest_problems_problem
    ON contest_problems(problem_id,created_at DESC);
CREATE INDEX idx_contest_build_items_job_ordinal
    ON contest_build_items(job_id,ordinal);
CREATE INDEX idx_contest_build_items_materialization
    ON contest_build_items(materialization_id);

COMMIT;
PRAGMA foreign_keys=ON;
PRAGMA foreign_key_check;
PRAGMA integrity_check;
SQL
```

`foreign_key_check` must print no rows and `integrity_check` must print `ok`.
Start the new application only after both checks and the row-count comparison
succeed. Remove any temporary command file outside the repository; retain the
backup until the upgraded deployment has been verified.

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
