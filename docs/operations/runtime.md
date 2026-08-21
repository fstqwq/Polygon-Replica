# Runtime and deployment

This document defines runtime constraints. Installation, TLS, upgrades, and recovery are covered by the [deployment runbook](deployment.md).

## Supported topology

Run one application process. The worker queue, judgehost registry, runtime cache, and admission state are process-local; multiple uvicorn workers or application replicas are unsupported.

Production terminates HTTPS at a TLS proxy and exposes uvicorn only on loopback. The browser password flow requires HTTPS outside localhost, and `AUTH_COOKIE_SECURE` defaults to `true`. Systemd and Compose bind host port `8001` to loopback and trust forwarded headers from the direct proxy peer.

Generated judgehost commands use `domjudge/judgehost:latest`. The [judgehost image guide](deployment.md#judgehost-image-choice) documents the modified image for long-running stability.

## Host installation

`scripts/install_host.sh` supports Debian and Ubuntu hosts with a regular GIL-enabled CPython 3.14 interpreter. It installs dependencies, prepares storage and user namespaces, verifies bubblewrap, TeX, fonts, and statement conversion tools, creates `.venv`, and installs the systemd unit. The application runs as a non-root service account; direct root invocation must name that account through `POLYGON_REPLICA_RUNTIME_USER`.

## Startup and shutdown

| Phase | Behavior |
| --- | --- |
| Schema | Initialize an empty database, or validate an existing schema read-only. Missing required objects produce a schema-blocked `503`; upgrades are offline. |
| Recovery | Fail interrupted verification and package work, invalidate previews, and clear process-local runtime state. Recovery failure aborts startup. |
| Start | Load durable configuration and start worker threads after recovery and cache cleanup complete. |
| Shutdown | Stop admission and workers. Interrupted work becomes terminal during the next startup. |

## Docker

The production Compose service mounts durable source, SQLite, derived/cache data, and a separately managed backup root. Bubblewrap requires host user-namespace support; the checked-in service disables container seccomp and AppArmor confinement for the application. The image includes CPython 3.14, TeX and template fonts, Pandoc, Poppler, and librsvg. Required TeX initialization failures stop the build.

## Operations

Maintenance admission has three process-local states:

| State | Behavior |
| --- | --- |
| `open` | Admit normal requests and work. |
| `draining` | Reject new work while active requests, workers, judgehost cases, callbacks, and counted admin reads finish. |
| `closed` | Allow one exclusive cleanup, backup, or supervised restart operation. |

Cleanup and backup close ordinary and judgehost callback admission. Their storage effects are defined by the [storage protocol](../protocol/storage.md). Active cache directories must not be removed manually.
