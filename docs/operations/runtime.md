# Runtime and deployment

This document describes runtime constraints. For executable host and Compose
procedures, TLS setup, first use, upgrades, and recovery, use the
[operator deployment runbook](deployment.md).

## Supported topology

Run one application process. The scheduler, host registry, runtime cache,
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
dependencies, configures user namespaces, creates storage roots, probes
bubblewrap and TeX as the runtime account, creates `.venv`, writes the bootstrap
environment file, and installs the systemd unit. Required TeX initialization is
fail-closed. TeX Gyre and Noto CJK fonts used by the canonical statement template
are explicit installer dependencies, and the XeLaTeX probe resolves both font
families as the runtime account. The environment file is atomically replaced as
`root:root` mode `0600`; installer-managed paths are refreshed while other valid
assignments are preserved as systemd `NAME=VALUE` records without executing the
old file. An optional shell `export` prefix in an existing assignment is accepted
only as migration input and is removed from the rendered file. Both `#` and `;`
comment lines are preserved.

The invocation account is normally the service account. An invocation through
`sudo` uses the original `SUDO_USER`; a direct root invocation MUST set
`POLYGON_REPLICA_RUNTIME_USER` to an existing non-root account. The installer
refuses a root runtime. It validates the account and group, runs probes and
Python installation as that account, owns writable roots with it, renders quoted
systemd paths, and verifies the unit before installation. Application runtime
does not require root.

## Startup and shutdown

Startup initializes metadata, marks interrupted package/export work failed,
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
contest source data, cleanup-safe cache/artifact data, and a separately managed
backup root. The backup root must not be placed inside a disposable cache
volume.

Bubblewrap inside a container requires host user-namespace support. The checked-
in Compose service disables seccomp and AppArmor confinement for the application
container; this is part of its current deployment security boundary. The image
installs the same TeX Gyre and Noto CJK template fonts explicitly. Required TeX
database, format, and font-map initialization failures stop the image build.
`docker-compose.e2e.yml` is test infrastructure, not a production retention
model.

## Operations

Admin operations include Judgehost status and enablement, users, SMTP/system
configuration, and exclusive artifact cleanup. Do not manually delete active
cache subtrees while the process is running. Observe process health, worker
capacity, Judgehost leases, domain job status, artifact availability, disk
space, and backup age as separate signals.

Exclusive cleanup closes Judgehost callback admission as well as ordinary work
admission. It remains busy until in-flight callbacks have released their
receipts; callbacks arriving after the gate closes are acknowledged without
touching scheduler, database, or blob state.
