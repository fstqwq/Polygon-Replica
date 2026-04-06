# Polygon-Replica

Polygon-Replica is a self-hosted Polygon-like problem preparation system.

It uses:

- Git as the source of truth for problem files
- a database for metadata and runtime state
- local filesystem storage for derived artifacts
- a web UI as the primary workflow

## What It Does

- Edit problem assets:
  - statement
  - checker
  - validator
  - interactor
  - generators
  - tests
  - solutions
- Import packages:
  - Polygon
  - ICPC
  - native
- Export packages:
  - ICPC
  - native
- Run verification and custom runs through judgehost / judgedaemon
- Build contest-level PDFs and handle contest file overrides

## Quick Start

Deploy the repository to a Linux host, then run:

```bash
cd /opt/polygon-replica
bash scripts/install_host.sh
```

The installer:

- installs system dependencies
- creates the runtime directories
- creates `.venv`
- writes `/etc/polygon-replica.env`
- installs and starts `polygon-replica.service`

After installation, the app should be reachable on:

- `http://127.0.0.1:8001`

On first access, the system redirects to `/setup` until the first administrator account is created.

## Development

For local fallback development on a Linux or WSL environment:

```bash
./scripts/start_local.sh
```

This starts the app directly with `uvicorn` using the current default runtime layout.

## Tests

Main test entrypoint:

```bash
./tests/scripts/test.sh
```

Package import smoke tests:

```bash
./tests/scripts/import-smoke.sh
```

To include slow UI tests:

```bash
POLYGON_REPLICA_INCLUDE_SLOW_TESTS=1 ./tests/scripts/test.sh
```

## Runtime Requirements

- Python 3
- Git
- TeX Live
- `bubblewrap`
- `seccomp`
- at least one DOMjudge judgedaemon for verification and run execution

Without a judgedaemon:

- the web UI still works
- file editing and Git operations still work
- verification and run execution remain queued
