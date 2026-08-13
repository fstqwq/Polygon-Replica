# Polygon Replica

Polygon Replica is a self-hosted system for authoring, reviewing, verifying,
and delivering programming-contest problems. It provides everything a team
needs to turn an initial solution and a few tests into a contest-ready problem,
plus an agent interface
([Polygon-Skills](https://github.com/fstqwq/Polygon-Skills)) for automating the
same workflow.

Polygon Replica uses [DOMjudge](https://www.domjudge.org/) Judgehosts as its
execution backend, giving multi-pass and interactive problems the same
first-class verification workflow as conventional batch problems.

## What you can do

- Create a problem from scratch or import a Polygon package, an ICPC Problem
  Package, or a Polygon Replica archive.
- Edit statements, tests, generators, validators, checkers, interactors,
  solutions, attachments, and configuration in a per-user workspace.
- Compile multilingual LaTeX statements and preview samples.
- Run generated and manual tests through trusted DOMjudge Judgehosts.
- Verify accepted, wrong-answer, time-limit, runtime-error, and rejected
  solutions against the same generated tests.
- Review workspace changes and publish the next official version of a problem.
- Verify an official version once, download its Polygon Replica package, and
  project it into either a DOMjudge package or a strict ICPC Problem Package
  2025-09 without rerunning the tests.
- Organize problems into contests, manage contest membership, inspect
  readiness, and build statement PDFs, DOMjudge bundles, or ICPC 2025-09
  bundles from fixed verified revisions.
- Use the browser-first workflow or automate editing, verification, export, and
  downloads through the Agent API and Polygon Agent CLI.

## The workflow

```text
create or import
      |
      v
per-user workspace ---- verify and review
      |
      | publish
      v
official problem version ---- full verification ---- verified revision
                                                        |
                         +------------------------------+-------------------+
                         |                 |                    |           |
                         v                 v                    v           v
             Polygon Replica package  DOMjudge package  ICPC 2025-09  Contest builds
```

A workspace is a private working copy belonging to one user. Publishing saves
its reviewed changes as the next official version of the problem. Internally,
each version is a Git commit on the problem's `main` branch, so the exact source
used for a package can always be identified. Generated inputs, answers, logs,
PDFs, and archives can be cleaned and rebuilt without deleting that history.

After a full verification succeeds, that official version becomes a
**verified revision**. It retains the exact published source together with the
generated test inputs and official answers used by the run. Its Polygon Replica
package is the system's own downloadable serialization. DOMjudge and ICPC
2025-09 packages are projections of the same verified revision, not separate
verification results.

Contest builds consume only verified revisions that already exist. They never
start a problem verification implicitly. A Contest can therefore use the
latest verified revision even when it trails the newest published version, and
its readiness page makes that distinction visible.

## Quick start with Docker Compose

Polygon Replica runs on Linux. The application sandbox uses unprivileged user
namespaces, so enable them on the Docker host first:

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

Clone the repository, create the permanent backup bind, and start the app:

```bash
git clone https://github.com/fstqwq/Polygon-Replica.git
cd Polygon-Replica

sudo install -d -o 1000 -g 1000 -m 0700 /var/backups/polygon-replica
umask 077
printf '%s\n' \
  'POLYGON_REPLICA_BACKUP_HOST_DIR=/var/backups/polygon-replica' >.env

sudo docker compose up -d --build
sudo docker compose logs --tail=200 app
```

Open <http://127.0.0.1:8001/>, complete first-run setup, and create the
administrator. Then configure Judgehost credentials in Settings and use the
generated command to start at least one Judgehost. Authoring and statement
preview work without a Judgehost; verification and executable builds require
one.

For an Internet-facing installation, do not expose port 8001 directly. Follow
the [deployment runbook](docs/operations/deployment.md) for a TLS proxy,
systemd installation, persistent secrets, upgrades, backups, and recovery.

## Documentation

- [Documentation index](docs/README.md)
- [Product scope and rationale](docs/product.md)
- [System design](docs/design/system.md)
- [Problem source format](docs/protocol/problem-source.md)
- [Execution and verification](docs/protocol/execution.md)
- [Package import and export](docs/protocol/package.md)
- [Access model](docs/design/access.md)
- [Storage and cleanup](docs/protocol/storage.md)
- [Configuration](docs/operations/configuration.md)
- [Testing policy](docs/testing.md)

The [package protocol](docs/protocol/package.md) lists supported import and
export formats, the [execution protocol](docs/protocol/execution.md) explains
verification, and the [storage protocol](docs/protocol/storage.md) describes
the source, derived, and cache data classes.
