# Runtime and deployment

Runtime constraints are documented here. Host and Compose installation, TLS,
first use, upgrades, and recovery are covered by the
[operator deployment runbook](deployment.md).

## Supported topology

Run one application process. The batch/task runtime, host registry, runtime cache,
authentication throttling, and worker queue contain process-local state, so
multiple uvicorn workers or application replicas are not supported.

Production places a TLS proxy in front of the loopback uvicorn listener.
Judgehost traffic reaches the same process through authenticated `/api/v4/*`
routes. The systemd and Compose launchers bind host port `8001` to loopback and
configure uvicorn to accept forwarded headers from any direct peer. Keeping that
listener private is therefore part of the deployment security boundary.
The generated Judgehost command currently uses `domjudge/judgehost:latest`;
operators therefore control image pinning and upgrades outside the application.

## Host installation

`scripts/install_host.sh` supports Linux hosts using `apt-get`. It installs
system dependencies, configures user namespaces, creates storage roots, probes
bubblewrap and TeX as the runtime account, creates `.venv`, writes the bootstrap
environment file, and installs the systemd unit. Required TeX initialization is
fail-closed. Pandoc, Poppler and librsvg provide the best-effort HTML Statement
Preview conversion and controlled image conversion. TeX Gyre and Noto CJK fonts
used by the canonical statement template are explicit installer dependencies,
and the XeLaTeX probe resolves both font families as the runtime account. The
environment file is atomically replaced as
`root:root` mode `0600`; installer-managed paths are refreshed while other valid
assignments are preserved as systemd `NAME=VALUE` records without executing the
file. Input assignments may use an optional shell `export` prefix; output uses
systemd assignment syntax. Both `#` and `;` comment lines are preserved.

The host must provide the regular GIL-enabled `python3.14` interpreter before
the installer runs. The installer rejects any other Python minor version and
recreates `.venv` from that interpreter.

The invocation account is normally the service account. An invocation through
`sudo` uses the original `SUDO_USER`; a direct root invocation MUST set
`POLYGON_REPLICA_RUNTIME_USER` to an existing non-root account. The installer
refuses a root runtime. It validates the account and group, runs probes and
Python installation as that account, owns writable roots with it, renders quoted
systemd paths, and verifies the unit before installation. Application runtime
does not require root.

## Startup and shutdown

`app.runtime.ApplicationRuntime` owns concrete service construction, and
`app/runtime_lifecycle.py` receives it explicitly for startup and shutdown. A
new empty database is initialized with the current schema. An existing database
is checked read-only before services consume it. If a required table, column,
or named index is missing, runtime startup is skipped and HTTP serves a raw
`503` listing the missing objects; the operator must stop the service, upgrade
SQLite offline, and restart. The application never upgrades an existing
database automatically. Extra schema objects and rows are tolerated.

After schema admission, startup initializes metadata, invalidates every
Statement Preview cache record, marks interrupted
package/export work failed,
applies durable configuration, and atomically terminalizes unfinished
verification parents and tasks before deleting their runtime blobs. A failed
verification recovery aborts startup. It then reconciles other unfinished
domain work, clears startup-scoped caches and worker history, and starts worker
threads. Process-local jobs cannot resume across restart.

Shutdown stops the worker queue. Operators should expect interrupted
asynchronous jobs to become terminal during the next startup reconciliation and
be requested again.

## Docker

The production Compose service mounts durable Git/workspace data, SQLite and
Contest source data, cache/derived data, and a separately managed
backup root. The backup root must not be placed inside a disposable cache
volume.

Bubblewrap inside a container requires host user-namespace support. The checked-
in Compose service disables seccomp and AppArmor confinement for the application
container; this is part of its current deployment security boundary. The image
uses the regular GIL-enabled CPython 3.14 image and installs the same TeX Gyre
and Noto CJK template fonts explicitly. Required TeX
database, format, and font-map initialization failures stop the image build.
The image also installs Pandoc, Poppler and librsvg; HTML conversion does not
use an external MathJax CDN.
`tests/docker_e2e/docker-compose.e2e.yml` is test infrastructure, not a production retention
model.

## Operations

Admin operations include Judgehost status and enablement, users, SMTP/system
configuration, exclusive generated-data cleanup, and exclusive recovery backup.
The backup publishes one archive containing a consistent SQLite snapshot plus
the bare Git, workspace, and Contest source roots. It excludes generated
artifacts, caches, deployment configuration, and secrets outside SQLite. Do not
manually delete active cache subtrees while the process is running. Observe
process health, worker capacity, Judgehost leases, domain job status,
derived-product integrity, disk space, and backup age as separate signals.

Maintenance admission has three process-local states. `open` admits normal
requests and jobs. `draining` rejects new business work while admitted workers,
Judgehost dispatch, callbacks, and counted Admin reads finish; Admin pages stay
available and an empty Judgehost fetch skips long polling. `closed` is used only
while cleanup, backup, or a supervised restart owns the process. Cleanup removes
the configured derived and cache roots plus their cleanup-safe SQLite rows; Git,
workspaces, Contest source, and backup roots are outside that inventory.

Both exclusive operations close Judgehost callback admission as well as
ordinary work admission. They remain busy until in-flight callbacks have
released their receipts; callbacks arriving after the gate closes are
acknowledged without touching batch runtime, database, or blob state.
