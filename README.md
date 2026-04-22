# Polygon-Replica

Polygon-Replica is a self-hosted replacement for the Codeforces Polygon workflow.
It is for users who already know [Codeforces Polygon](https://polygon.codeforces.com)
and want to move that workflow onto infrastructure they control.

Key features:

- Existing Polygon packages can be imported, edited, verified, and exported again.
- DOMjudge-style judgedaemon execution infrastructure. Runs can reuse judgehosts that are close to the final onsite judging environment, and interactive/multi-pass problems are supported natively.
- Agent-friendly workflows with companion `Polygon-Skills`. AI assistants can support problem authoring, review, verification, export, and operations.

## Implementation Characteristics

Polygon-Replica separates authored sources from runtime state and generated artifacts.

At a high level:

- Problem sources are Git-backed, and each user edits through an isolated workspace.
- SQLite stores metadata and job state; generated artifacts stay outside Git on local filesystem roots.
- Verification, custom runs, exports, and contest builds run as async worker jobs.
- All code compilations, testcase generations, and judgings run through DOMjudge-style judgedaemon infrastructure.

This keeps authored problem files versioned while keeping generated PDFs, verification outputs, snapshots, and export archives outside Git.

## Deployment

For deployment, see [docs/deployment.md](docs/deployment.md).

DOMjudge judgedaemon workers can run anywhere with network access to the main app
using the `domjudge/judgehost:latest` Docker image.

## Documentation

Useful references:

- [Deployment](docs/deployment.md)
- [Agent and contributor requirements](docs/AGENTS.md)
- [System architecture](docs/architecture.md)
- [Problem editing and workspace model](docs/problem-workflow.md)
- [Database schema and data patterns](docs/data-model.md)
- [Request lifecycle and auth](docs/request-lifecycle.md)
- [Verification, runs, and judgehost integration](docs/verification-and-runs.md)
