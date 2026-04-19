# Polygon-Replica

Polygon-Replica is a self-hosted replacement for the Codeforces Polygon workflow.

It is for users who already know [Codeforces Polygon](https://polygon.codeforces.com) and want to move that workflow onto infrastructure they control. Existing Polygon packages can be imported, edited, verified, and exported again without sending the problem-authoring process back to polygon.codeforces.com.

## Polygon Migration

The goal is a smooth migration path from Codeforces Polygon, not a new problem format that forces users to rebuild their habits from scratch.

Polygon-Replica supports the usual problem-authoring loop:

- import an existing Polygon package or create a new problem
- edit the problem through the Web UI
- run verification and custom runs
- export native and ICPC packages
- prepare contest-level packages and PDFs

The project does not try to reproduce every Codeforces Polygon page or internal behavior. It focuses on preserving the workflow that matters for preparing and delivering problems.

## Judgehost Integration

Polygon-Replica uses DOMjudge-style judgedaemon infrastructure for execution.

Judgehosts can run on separate machines as long as they can reach the Polygon-Replica server over HTTP. The same judgehost setup can be used for problem verification and contest runs.

This lets Polygon-Replica reuse the execution capabilities of the onsite judging environment, including interactive and multi-pass problems when the judgedaemon setup supports them.

The judgehost-facing API lives under:

```text
/api/v4/*
```

## AI Agent Workflow

Polygon-Replica is designed to work with the companion `Polygon-Skills` repository.

The skills give Claude, Codex, and similar agents a stable way to help with problem preparation. Agents can follow fixed workflows for authoring, reviewing, importing, exporting, verifying, and operating against a running Polygon-Replica server.

The Web UI remains the main human interface. The skills provide an agent interface over the same problem structure and workflow conventions.

## Implementation Characteristics

Polygon-Replica separates authored sources from runtime state and generated artifacts.

At a high level:

- problem sources live in Git-backed repositories
- each user edits through a workspace
- SQLite stores users, permissions, metadata, job state, verification state, and export state
- generated artifacts live on the filesystem
- statement preview is compiled synchronously
- verification, custom runs, exports, and contest builds run as async worker jobs
- judgehost-compatible execution is exposed under `/api/v4/*`

This keeps authored problem files versioned while keeping generated PDFs, verification outputs, snapshots, and export archives outside Git.

## Deployment

For production-style deployment, see:

```text
../DEPLOY.md
```

The standard production setup uses Linux, systemd, nginx, Python 3, Git, TeX Live, bubblewrap / seccomp, and DOMjudge judgedaemon workers.

## Local Development

For local development on Linux or WSL:

```bash
./scripts/start_local.sh
```

The app runs with the default local runtime layout.

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

## Documentation

Useful references:

- `docs/architecture.md`
- `docs/problem-workflow.md`
- `docs/data-model.md`
- `docs/request-lifecycle.md`
- `docs/verification-and-runs.md`
- `../DEPLOY.md`
