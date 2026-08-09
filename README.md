# Polygon-Replica

Polygon-Replica is a self-hosted problem-authoring system with a web-first
workflow, Git-backed problem sources, SQLite metadata, local derived artifacts,
and DOMjudge-compatible Judgehost workers.

The current runtime is one FastAPI application served by uvicorn. Statement
preview compilation is synchronous. Verification, custom run, export, and
contest build jobs share a process-local worker queue. Published problem source
is the `main` commit of the problem's bare Git repository; workspaces are mutable
per-user checkouts.

## Documentation

- [Documentation index](docs/README.md)
- [System design](docs/design/system.md)
- [Problem source protocol](docs/protocol/problem-source.md)
- [Execution protocol](docs/protocol/execution.md)
- [Judgehost protocol](docs/protocol/judgehost.md)
- [Storage and cleanup](docs/protocol/storage.md)
- [Package import and export](docs/protocol/package.md)
- [Application package map](docs/src/README.md)
- [Known implementation findings](docs/implementation/findings.md)

These documents describe the current implementation. Proposed behavior belongs
in a dedicated proposal, not in current architecture or protocol documents.
